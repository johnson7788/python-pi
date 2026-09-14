"""Accounts, cookie sessions and team-mode enforcement.

Two layers are covered here on purpose.  The unit half drives the database and the
auth primitives directly, where the interesting cases live (expiry, revocation, a
disabled account, a tampered hash).  The HTTP/WebSocket half runs the **real server**
in team mode as a subprocess, because the property that matters most — that an
anonymous caller cannot reach anything — is a property of the wiring, not of the
functions.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import websockets
from conftest import write_agent_config

from pi_server.auth import (
    authenticate,
    ensure_admin,
    hash_password,
    hash_token,
    new_session_token,
    resolve_session,
    start_session,
    verify_password,
)
from pi_server.config import COOKIE_NAME, ServerConfig
from pi_server.db import Database, DuplicateUsername

REPO_ROOT = Path(__file__).resolve().parent.parent

ADMIN_PASSWORD = "admin-password-1"
USER_PASSWORD = "user-password-1"

# --------------------------------------------------------------------------------------
# Database and primitives
# --------------------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path) -> Database:
    database = Database(str(tmp_path / "pi.db"))
    try:
        yield database
    finally:
        database.close()


@pytest.fixture()
def config(tmp_path) -> ServerConfig:
    return ServerConfig(data_root=str(tmp_path / "data"), auth_mode="team")


def test_migrations_are_idempotent(tmp_path):
    path = str(tmp_path / "pi.db")
    first = Database(path)
    version = first.schema_version
    first.close()

    second = Database(path)
    try:
        assert version >= 1
        assert second.schema_version == version
    finally:
        second.close()


def test_password_hash_roundtrip_and_rejection():
    encoded = hash_password("correct horse battery staple")

    assert encoded.startswith("scrypt$")
    assert encoded != hash_password("correct horse battery staple")  # fresh salt each time
    assert verify_password("correct horse battery staple", encoded) is True
    assert verify_password("wrong", encoded) is False
    # Malformed input is a failed login, never an exception.
    assert verify_password("x", "not-a-hash") is False
    assert verify_password("x", "scrypt$a$b$c$d$e") is False


def test_usernames_are_unique(db):
    db.create_user(username="ada", password_hash=hash_password("pw-12345678"))
    with pytest.raises(DuplicateUsername):
        db.create_user(username="ada", password_hash=hash_password("pw-12345678"))


def test_update_user_refuses_unknown_columns(db):
    uid = db.create_user(username="ada", password_hash=hash_password("pw-12345678"))
    with pytest.raises(ValueError):
        db.update_user(uid, password="oops")


def test_authenticate_covers_unknown_wrong_and_disabled(db):
    uid = db.create_user(username="ada", password_hash=hash_password(USER_PASSWORD))

    assert authenticate(db, "nobody", USER_PASSWORD) is None
    assert authenticate(db, "ada", "wrong-password") is None
    assert authenticate(db, "ada", USER_PASSWORD) is not None

    db.update_user(uid, status="disabled")
    assert authenticate(db, "ada", USER_PASSWORD) is None


def test_session_lifecycle_expiry_revocation_and_disable(db, config):
    uid = db.create_user(username="ada", password_hash=hash_password(USER_PASSWORD))
    user = authenticate(db, "ada", USER_PASSWORD)
    assert user is not None

    token = start_session(db, user, config)
    assert resolve_session(db, token) is not None
    assert resolve_session(db, "not-a-real-token") is None

    # Revocation is immediate: that is the whole reason for server-side sessions.
    db.revoke_auth_session(hash_token(token))
    assert resolve_session(db, token) is None

    # An expired session is refused even though the row still exists.
    expired = new_session_token()
    db.create_auth_session(
        token_hash=hash_token(expired), uid=uid, expires_at="2000-01-01T00:00:00+00:00"
    )
    assert resolve_session(db, expired) is None

    # Disabling the account kills live sessions without touching any token.
    live = start_session(db, user, config)
    db.update_user(uid, status="disabled")
    assert resolve_session(db, live) is None


def test_ensure_admin_only_creates_the_first_account(db):
    created = ensure_admin(db, "admin", ADMIN_PASSWORD)
    assert created == ("admin", ADMIN_PASSWORD)
    assert db.get_user_by_username("admin")["role"] == "superadmin"
    assert db.get_user_by_username("admin")["must_change_password"] == 0

    assert ensure_admin(db, "admin", "another-password") is None
    assert db.count_users() == 1


def test_ensure_admin_generates_a_password_that_must_be_changed(db):
    created = ensure_admin(db, "admin")
    assert created is not None
    username, generated = created
    assert username == "admin"
    assert len(generated) >= 12
    assert db.get_user_by_username("admin")["must_change_password"] == 1


# --------------------------------------------------------------------------------------
# The real server, in team mode
# --------------------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class TeamServer:
    """A running ``pi-py-web`` in team mode, plus its data root."""

    def __init__(self, proc: subprocess.Popen, port: int, data_root: Path) -> None:
        self.proc = proc
        self.port = port
        self.data_root = data_root
        self.base_url = f"http://127.0.0.1:{port}"
        self.ws_url = f"ws://127.0.0.1:{port}/api/ws"

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)

    def stderr(self) -> str:
        if self.proc.stderr is None:
            return ""
        try:
            return self.proc.stderr.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return ""


def _start_team_server(tmp_path: Path, base_url: str) -> TeamServer:
    agent_dir = tmp_path / "agent"
    workdir = tmp_path / "work"
    workdir.mkdir()
    write_agent_config(agent_dir, base_url)

    data_root = tmp_path / "data"
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "pi_server.cli",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--cwd",
            str(workdir),
            "--agent-dir",
            str(agent_dir),
            "--model",
            "local/local-test",
            "--auth-mode",
            "team",
            "--data-root",
            str(data_root),
            "--admin-username",
            "admin",
            "--admin-password",
            ADMIN_PASSWORD,
        ],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    server = TeamServer(proc, port, data_root)

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"server exited early:\n{server.stderr()}")
        try:
            if httpx.get(f"{server.base_url}/api/health", timeout=1.0).status_code == 200:
                return server
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(0.1)
    server.stop()
    raise AssertionError(f"server never became healthy:\n{server.stderr()}")


@pytest.fixture()
def team_server(fake_openai, tmp_path):
    _, base_url = fake_openai
    server = _start_team_server(tmp_path, base_url)
    try:
        yield server
    finally:
        server.stop()


def _login(server: TeamServer, username: str, password: str) -> httpx.Client:
    """A client that keeps the session cookie, like a browser."""
    client = httpx.Client(base_url=server.base_url, timeout=10)
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return client


def test_health_is_public_and_reports_the_mode(team_server):
    health = httpx.get(f"{team_server.base_url}/api/health", timeout=10).json()
    assert health["status"] == "ok"
    assert health["authMode"] == "team"


def test_anonymous_callers_are_refused_everywhere_under_api(team_server):
    for path in ("/api/state", "/api/sessions", "/api/models", "/api/admin/users", "/api/auth/me"):
        response = httpx.get(f"{team_server.base_url}{path}", timeout=10)
        assert response.status_code == 401, path
        assert response.json()["error"] == "unauthenticated"


def test_login_rejects_a_wrong_password_and_sets_nothing(team_server):
    response = httpx.post(
        f"{team_server.base_url}/api/auth/login",
        json={"username": "admin", "password": "not-the-password"},
        timeout=10,
    )
    assert response.status_code == 401
    assert COOKIE_NAME not in response.cookies
    # The message must not distinguish "no such user" from "wrong password".
    assert response.json() == {"detail": {"error": "invalid username or password"}}


def test_login_then_reach_the_protected_surface(team_server):
    client = _login(team_server, "admin", ADMIN_PASSWORD)
    try:
        me = client.get("/api/auth/me").json()
        assert me["authMode"] == "team"
        assert me["user"]["username"] == "admin"
        assert me["user"]["isAdmin"] is True
        assert "password_hash" not in me["user"]

        assert client.get("/api/state").status_code == 200
        assert client.get("/api/sessions").status_code == 200
    finally:
        client.close()


def test_logout_invalidates_the_session_immediately(team_server):
    client = _login(team_server, "admin", ADMIN_PASSWORD)
    try:
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/state").status_code == 401
    finally:
        client.close()


def test_members_api_enforces_roles(team_server):
    admin = _login(team_server, "admin", ADMIN_PASSWORD)
    try:
        created = admin.post(
            "/api/admin/users",
            json={"username": "ada", "password": USER_PASSWORD, "displayName": "Ada"},
        )
        assert created.status_code == 200, created.text
        assert created.json()["user"]["role"] == "user"
        assert "password_hash" not in created.json()["user"]

        # A non-admin may neither list nor create accounts.
        member = _login(team_server, "ada", USER_PASSWORD)
        try:
            assert member.get("/api/admin/users").status_code == 403
            assert member.post(
                "/api/admin/users", json={"username": "eve", "password": USER_PASSWORD}
            ).status_code == 403
        finally:
            member.close()

        # Only a superadmin may mint another administrator.
        assert admin.patch(
            "/api/admin/users/" + created.json()["user"]["uid"], json={"role": "admin"}
        ).status_code == 200
    finally:
        admin.close()


def test_an_administrator_cannot_lock_themselves_out(team_server):
    admin = _login(team_server, "admin", ADMIN_PASSWORD)
    try:
        uid = admin.get("/api/auth/me").json()["user"]["uid"]
        assert admin.patch(f"/api/admin/users/{uid}", json={"status": "disabled"}).status_code == 400
        assert admin.patch(f"/api/admin/users/{uid}", json={"role": "user"}).status_code == 400
        # Still works, which is the point.
        assert admin.get("/api/state").status_code == 200
    finally:
        admin.close()


def test_disabling_an_account_kills_its_sessions(team_server):
    admin = _login(team_server, "admin", ADMIN_PASSWORD)
    member = None
    try:
        created = admin.post(
            "/api/admin/users", json={"username": "ada", "password": USER_PASSWORD}
        ).json()["user"]
        member = _login(team_server, "ada", USER_PASSWORD)
        assert member.get("/api/state").status_code == 200

        assert admin.patch(
            f"/api/admin/users/{created['uid']}", json={"status": "disabled"}
        ).status_code == 200
        assert member.get("/api/state").status_code == 401
    finally:
        if member is not None:
            member.close()
        admin.close()


def test_changing_your_password_signs_other_browsers_out(team_server):
    admin = _login(team_server, "admin", ADMIN_PASSWORD)
    other = _login(team_server, "admin", ADMIN_PASSWORD)
    try:
        response = admin.post(
            "/api/auth/password",
            json={"currentPassword": ADMIN_PASSWORD, "newPassword": "brand-new-password"},
        )
        assert response.status_code == 200, response.text

        assert admin.get("/api/state").status_code == 200       # this browser carries on
        assert other.get("/api/state").status_code == 401       # the other one does not
        assert admin.post(
            "/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}
        ).status_code == 401
    finally:
        other.close()
        admin.close()


def test_websocket_requires_a_session_cookie(team_server):
    async def anonymous() -> int | None:
        try:
            async with websockets.connect(team_server.ws_url) as ws:
                await ws.recv()
        except websockets.exceptions.ConnectionClosed as error:
            # `rcvd` is the close frame the server sent; its code is what we asserted.
            return error.rcvd.code if error.rcvd is not None else None
        return None

    async def authenticated(cookie: str) -> dict:
        async with websockets.connect(
            team_server.ws_url, additional_headers={"Cookie": cookie}
        ) as ws:
            return json.loads(await ws.recv())

    client = _login(team_server, "admin", ADMIN_PASSWORD)
    try:
        assert asyncio.run(anonymous()) == 4401

        cookie = f"{COOKIE_NAME}={client.cookies[COOKIE_NAME]}"
        ready = asyncio.run(authenticated(cookie))
        assert ready["type"] == "ready"
    finally:
        client.close()
