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
import time

import httpx
import pytest
import websockets
from conftest import (
    TEAM_ADMIN_PASSWORD,
    TEAM_USER_PASSWORD,
    TeamServer,
    cookie_header,
    login,
    reset_fake_model,
    team_server,  # noqa: F401 - re-exported so pytest can see the fixture
    ws_prompt,
)

from pi_coding_agent.bootstrap import BootstrapOptions
from pi_server.app import CapacityError, SessionRegistry, SessionWorker
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

ADMIN_PASSWORD = TEAM_ADMIN_PASSWORD
USER_PASSWORD = TEAM_USER_PASSWORD

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


def _login(server: TeamServer, username: str, password: str) -> httpx.Client:
    return login(server, username, password)


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

        cookie = cookie_header(client)
        ready = asyncio.run(authenticated(cookie))
        assert ready["type"] == "ready"
    finally:
        client.close()


# --------------------------------------------------------------------------------------
# M1: one worker per user, with per-user cwd and agent dir
# --------------------------------------------------------------------------------------


def _cookie(client: httpx.Client) -> str:
    return cookie_header(client)


def _create_members(admin: httpx.Client, *usernames: str) -> dict[str, str]:
    """Create accounts and return ``{username: uid}``."""
    uids: dict[str, str] = {}
    for username in usernames:
        response = admin.post(
            "/api/admin/users", json={"username": username, "password": USER_PASSWORD}
        )
        assert response.status_code == 200, response.text
        uids[username] = response.json()["user"]["uid"]
    return uids


def test_two_users_share_nothing(team_server):
    """The M1 acceptance test: two colleagues running at once, fully separated."""
    admin = _login(team_server, "admin", ADMIN_PASSWORD)
    try:
        uids = _create_members(admin, "ada", "bob")
        ada = _login(team_server, "ada", USER_PASSWORD)
        bob = _login(team_server, "bob", USER_PASSWORD)
        try:
            ada_state = ada.get("/api/state").json()
            bob_state = bob.get("/api/state").json()

            # The tenant boundary is the workspace path, and it is derived from the uid.
            assert ada_state["cwd"] != bob_state["cwd"]
            assert ada_state["cwd"] == str(team_server.data_root / "workspaces" / uids["ada"])
            assert bob_state["cwd"] == str(team_server.data_root / "workspaces" / uids["bob"])

            # Each user's agent really writes into its own workspace.
            reset_fake_model()
            ws_prompt(team_server, _cookie(ada))
            reset_fake_model()
            ws_prompt(team_server, _cookie(bob))

            ada_workspace = team_server.data_root / "workspaces" / uids["ada"]
            bob_workspace = team_server.data_root / "workspaces" / uids["bob"]
            assert (ada_workspace / "e2e.txt").is_file()
            assert (bob_workspace / "e2e.txt").is_file()

            # Session lists come from each user's own agent dir, so neither sees the other.
            ada_sessions = ada.get("/api/sessions").json()["sessions"]
            bob_sessions = bob.get("/api/sessions").json()["sessions"]
            assert ada_sessions and bob_sessions
            assert ada_sessions[0]["path"] != bob_sessions[0]["path"]
            for listed in ada_sessions:
                assert str(team_server.data_root / "agents" / uids["ada"]) in listed["path"]
            for listed in bob_sessions:
                assert str(team_server.data_root / "agents" / uids["bob"]) in listed["path"]
        finally:
            ada.close()
            bob.close()
    finally:
        admin.close()


def test_a_template_agent_dir_is_seeded_but_never_carries_credentials(team_server):
    """Provider overrides must reach the user; a per-user credential file must not exist."""
    admin = _login(team_server, "admin", ADMIN_PASSWORD)
    try:
        uids = _create_members(admin, "ada")
        member = _login(team_server, "ada", USER_PASSWORD)
        try:
            assert member.get("/api/state").status_code == 200  # forces the worker to start
        finally:
            member.close()

        agent_dir = team_server.data_root / "agents" / uids["ada"]
        assert (agent_dir / "models.json").is_file()
        # The template that the fixture wrote *does* contain an auth.json; copying it would
        # have handed every user a per-user credential we do not manage.
        assert not (agent_dir / "auth.json").exists()
    finally:
        admin.close()


def test_each_user_sees_only_their_own_dialog_target(team_server):
    """The hub is per worker, so a dialog cannot surface in another user's tab."""
    # Exercised at the registry level: one Hub per worker is what makes the routing safe,
    # and this asserts it directly rather than depending on an extension being installed.
    config = ServerConfig(data_root=str(team_server.data_root), auth_mode="team")
    registry = SessionRegistry(BootstrapOptions(cwd="."), config)
    ada = SessionWorker("ada", BootstrapOptions(cwd="."))
    bob = SessionWorker("bob", BootstrapOptions(cwd="."))
    assert ada.hub is not bob.hub
    assert ada.hub.client_count == 0 and bob.hub.client_count == 0


def test_idle_eviction_skips_busy_and_recent_workers(tmp_path):
    config = ServerConfig(
        data_root=str(tmp_path), auth_mode="team", max_active_users=10, session_idle_minutes=1
    )
    registry = SessionRegistry(BootstrapOptions(cwd=str(tmp_path)), config)

    class FakeSession:
        is_streaming = True

    class FakeController:
        session = FakeSession()

    stale = SessionWorker("stale", BootstrapOptions(cwd=str(tmp_path)))
    stale.last_active = time.monotonic() - 3600
    busy = SessionWorker("busy", BootstrapOptions(cwd=str(tmp_path)))
    busy.last_active = time.monotonic() - 3600
    busy._controller = FakeController()  # type: ignore[assignment]
    recent = SessionWorker("recent", BootstrapOptions(cwd=str(tmp_path)))

    registry._workers.update({"stale": stale, "busy": busy, "recent": recent})
    assert asyncio.run(registry.evict_idle()) == 1
    assert registry.worker("stale") is None
    assert registry.worker("busy") is not None  # a running turn is never interrupted
    assert registry.worker("recent") is not None


def test_the_worker_cap_is_enforced(tmp_path):
    config = ServerConfig(
        data_root=str(tmp_path), auth_mode="team", max_active_users=1, session_idle_minutes=0
    )
    registry = SessionRegistry(BootstrapOptions(cwd=str(tmp_path)), config)
    registry._workers["someone"] = SessionWorker("someone", BootstrapOptions(cwd=str(tmp_path)))

    with pytest.raises(CapacityError):
        asyncio.run(registry.acquire("another"))


def test_single_mode_passes_the_command_line_through(tmp_path):
    """The personal flow must not acquire per-user directories by accident."""
    config = ServerConfig(data_root=str(tmp_path), auth_mode="single")
    base = BootstrapOptions(cwd="/tmp/somewhere", agent_dir="/tmp/agent")
    registry = SessionRegistry(base, config)
    assert registry.options_for("local") is base


def test_team_mode_scrubs_the_shell_environment(tmp_path):
    """Team mode must not hand the shared credential to a shell the agent runs."""
    config = ServerConfig(data_root=str(tmp_path), auth_mode="team")
    registry = SessionRegistry(BootstrapOptions(cwd="/tmp"), config)
    options = registry.options_for("someuid")
    assert options.scrub_shell_env is True
    assert options.no_extensions is True
    assert options.api_key is None  # no shared key configured in this unit test


def test_the_shell_is_public_but_the_data_is_not(team_server):
    """A logged-out browser must be able to load the page that asks it to log in.

    The SPA is served outside `/api/`, so it is reachable anonymously by design; every
    endpoint that carries a session, a file or an account is not. Getting this backwards
    would either break the login page or leak data.
    """
    shell = httpx.get(f"{team_server.base_url}/", timeout=10)
    assert shell.status_code in (200, 503)  # 503 only when the SPA was never built

    for path in ("/api/state", "/api/sessions", "/api/workspace/tree", "/api/usage/me"):
        assert httpx.get(f"{team_server.base_url}{path}", timeout=10).status_code == 401, path


# --------------------------------------------------------------------------------------
# A preset admin password
# --------------------------------------------------------------------------------------


def test_a_preset_admin_password_is_used_and_not_forced_to_change(fake_openai, tmp_path):
    """Presetting the password is only useful if it stays the one that was preset."""
    from conftest import start_team_server

    _, base_url = fake_openai
    password = "preset-admin-password-1"
    # No `--admin-password`: the point is the credential arriving from the environment.
    server = start_team_server(
        tmp_path, base_url, admin_password=None, extra_env={"PI_ADMIN_PASSWORD": password}
    )
    try:
        client = login(server, "admin", password)
        try:
            me = client.get("/api/auth/me").json()["user"]
            assert me["username"] == "admin"
            # A generated password must be changed at first login; a preset one must not, or
            # the operator's own credential would be refused on first use.
            assert me["mustChangePassword"] is False
            assert client.get("/api/state").status_code == 200
        finally:
            client.close()
    finally:
        server.stop()


def test_the_generated_path_still_forces_a_change(fake_openai, tmp_path):
    """The default must not change: with nothing preset, the printed password is one-time."""
    from conftest import start_team_server

    _, base_url = fake_openai
    # Neither the flag nor the variable, so the server must generate one itself.
    server = start_team_server(tmp_path, base_url, admin_password=None)
    server.stop()
    log = server.stderr()

    assert "created the first superadmin" in log
    assert "must be changed at first login" in log
