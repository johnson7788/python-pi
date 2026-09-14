"""The per-user workspace, and the path rules that keep it per-user.

The unit half attacks :class:`Workspace` directly — ``..``, absolute paths, NUL bytes and
a symlink planted inside the tree.  The HTTP half proves the wiring: two logged-in users
see disjoint trees, and an anonymous caller cannot reach the endpoints at all.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from conftest import (
    TEAM_ADMIN_PASSWORD,
    TEAM_USER_PASSWORD,
    cookie_header,
    login,
    team_server,  # noqa: F401 - re-exported so pytest can see the fixture
)

from pi_server.workspace import (
    MAX_PREVIEW_BYTES,
    InvalidPath,
    NotFound,
    TooLarge,
    Workspace,
    normalize_relative,
    valid_child_name,
)

# --------------------------------------------------------------------------------------
# Path handling
# --------------------------------------------------------------------------------------


@pytest.fixture()
def ws(tmp_path: Path) -> Workspace:
    return Workspace(str(tmp_path / "workspace"))


def test_normalize_relative_accepts_root_forms():
    assert normalize_relative("/") == ()
    assert normalize_relative("") == ()
    assert normalize_relative(None) == ()
    assert normalize_relative("/notes/todo.md") == ("notes", "todo.md")
    assert normalize_relative("notes/./todo.md") == ("notes", "todo.md")


@pytest.mark.parametrize(
    "hostile",
    ["/../etc/passwd", "..", "notes/../../etc/passwd", "a/../../b", "C:/Windows", "\x00"],
)
def test_normalize_relative_rejects_hostile_input(hostile):
    with pytest.raises(InvalidPath):
        normalize_relative(hostile)


def test_valid_child_name_rejects_separators():
    assert valid_child_name("report.md") == "report.md"
    for bad in ("", ".", "..", "a/b", "a\\b", "a\x00b"):
        with pytest.raises(InvalidPath):
            valid_child_name(bad)


def test_a_symlink_cannot_be_used_to_leave_the_workspace(ws: Workspace, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("classified", encoding="utf-8")
    os.symlink(outside, ws.root + "/link")

    with pytest.raises(InvalidPath):
        ws.path_for("/link/secret.txt", must_exist=True)
    with pytest.raises(InvalidPath):
        ws.read("/link/secret.txt")
    # Writing through the same door is refused too.
    with pytest.raises(InvalidPath):
        ws.write("/link/planted.txt", b"nope")
    assert not (outside / "planted.txt").exists()


def test_missing_workspace_is_reported(tmp_path: Path):
    with pytest.raises(NotFound):
        Workspace(str(tmp_path / "nope"), create=False)


# --------------------------------------------------------------------------------------
# Reads and writes
# --------------------------------------------------------------------------------------


def test_write_read_roundtrip_creates_parents(ws: Workspace):
    written = ws.write("/notes/2026/todo.md", "hello".encode())

    assert written["path"] == "/notes/2026/todo.md"
    assert written["size"] == 5
    assert ws.read("/notes/2026/todo.md")["text"] == "hello"


def test_read_detects_binary_and_flags_oversize(ws: Workspace):
    ws.write("/blob.bin", b"\xff\xfe\x00\x01")
    payload = ws.read("/blob.bin")
    assert payload["binary"] is True and payload["text"] is None

    ws.write("/big.txt", b"x" * (MAX_PREVIEW_BYTES + 10))
    large = ws.read("/big.txt")
    assert large["truncated"] is True
    assert large["text"] is None
    assert large["size"] == MAX_PREVIEW_BYTES + 10


def test_write_refuses_an_oversize_payload(ws: Workspace):
    with pytest.raises(TooLarge):
        ws.write("/big.bin", b"x" * (32 * 1024 * 1024 + 1))


def test_reading_a_directory_is_an_error(ws: Workspace):
    ws.create_directory("/", "notes")
    with pytest.raises(InvalidPath):
        ws.read("/notes")


def test_tree_lists_entries_and_marks_hidden_files(ws: Workspace):
    ws.create_directory("/", "notes")
    ws.write("/notes/todo.md", b"a")
    ws.write("/.env", b"SECRET=1")

    payload = ws.tree("/")
    names = {entry["name"] for entry in payload["entries"]}

    assert names == {"notes", ".env"}
    hidden = {entry["name"]: entry["hidden"] for entry in payload["entries"]}
    assert hidden == {"notes": False, ".env": True}
    assert payload["path"] == "/"


def test_tree_can_recurse_and_filter_to_files(ws: Workspace):
    ws.write("/a/deep.txt", b"a")
    ws.write("/b.txt", b"b")

    recursive = ws.tree("/", recursive=True)
    assert {entry["path"] for entry in recursive["entries"]} == {"/a", "/a/deep.txt", "/b.txt"}

    files_only = ws.tree("/", recursive=True, files_only=True)
    assert {entry["path"] for entry in files_only["entries"]} == {"/a/deep.txt", "/b.txt"}


def test_create_directory_rejects_duplicates_and_bad_names(ws: Workspace):
    ws.create_directory("/", "notes")
    with pytest.raises(InvalidPath):
        ws.create_directory("/", "notes")
    with pytest.raises(InvalidPath):
        ws.create_directory("/", "a/b")


def test_delete_removes_files_and_trees_but_never_the_root(ws: Workspace):
    ws.write("/a/deep.txt", b"a")
    ws.delete("/a")
    assert ws.tree("/")["entries"] == []

    with pytest.raises(InvalidPath):
        ws.delete("/")


def test_download_target_rejects_directories(ws: Workspace):
    ws.create_directory("/", "notes")
    with pytest.raises(InvalidPath):
        ws.download_target("/notes")
    ws.write("/notes/readme.md", b"hi")
    target, name = ws.download_target("/notes/readme.md")
    assert name == "readme.md" and Path(target).is_file()


# --------------------------------------------------------------------------------------
# Over HTTP, against the real server
# --------------------------------------------------------------------------------------


def _member(server, admin: httpx.Client, username: str) -> httpx.Client:
    response = admin.post(
        "/api/admin/users", json={"username": username, "password": TEAM_USER_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return login(server, username, TEAM_USER_PASSWORD)


def test_workspace_endpoints_refuse_anonymous_callers(team_server):
    for method, path in (
        ("get", "/api/workspace/tree"),
        ("get", "/api/workspace/file?path=/x"),
        ("put", "/api/workspace/file?path=/x"),
        ("delete", "/api/workspace/file?path=/x"),
        ("post", "/api/workspace/directory"),
        ("get", "/api/workspace/download?path=/x"),
    ):
        response = getattr(httpx, method)(f"{team_server.base_url}{path}", timeout=10)
        assert response.status_code == 401, (method, path)


def test_a_user_can_upload_edit_and_download_a_file(team_server):
    admin = login(team_server, "admin", TEAM_ADMIN_PASSWORD)
    try:
        member = _member(team_server, admin, "ada")
        try:
            uid = member.get("/api/auth/me").json()["user"]["uid"]

            # Raw bytes: this is the upload path, with no multipart parser involved.
            uploaded = member.put(
                "/api/workspace/file",
                params={"path": "/data/notes.txt"},
                content=b"raw bytes",
                headers={"Content-Type": "application/octet-stream"},
            )
            assert uploaded.status_code == 200, uploaded.text
            assert uploaded.json()["path"] == "/data/notes.txt"

            # It really landed in this user's workspace, and nowhere else.
            on_disk = team_server.workspace(uid) / "data" / "notes.txt"
            assert on_disk.read_bytes() == b"raw bytes"

            # JSON form: the text editor's path through the same endpoint.
            assert member.put(
                "/api/workspace/file", params={"path": "/data/notes.txt"}, json={"content": "edited"}
            ).status_code == 200
            assert member.get(
                "/api/workspace/file", params={"path": "/data/notes.txt"}
            ).json()["text"] == "edited"

            tree = member.get("/api/workspace/tree", params={"path": "/data"}).json()
            assert [entry["name"] for entry in tree["entries"]] == ["notes.txt"]

            download = member.get(
                "/api/workspace/download", params={"path": "/data/notes.txt"}
            )
            assert download.status_code == 200
            assert download.content == b"edited"
            assert "notes.txt" in download.headers.get("content-disposition", "")

            assert member.delete(
                "/api/workspace/file", params={"path": "/data/notes.txt"}
            ).status_code == 200
            assert not on_disk.exists()
        finally:
            member.close()
    finally:
        admin.close()


def test_two_users_have_disjoint_workspaces(team_server):
    admin = login(team_server, "admin", TEAM_ADMIN_PASSWORD)
    try:
        ada = _member(team_server, admin, "ada")
        bob = _member(team_server, admin, "bob")
        try:
            ada.put(
                "/api/workspace/file",
                params={"path": "/secret.txt"},
                content=b"ada only",
            )

            assert [e["name"] for e in ada.get("/api/workspace/tree").json()["entries"]] == [
                "secret.txt"
            ]
            assert bob.get("/api/workspace/tree").json()["entries"] == []
            # Bob cannot read it even knowing the name.
            assert bob.get(
                "/api/workspace/file", params={"path": "/secret.txt"}
            ).status_code == 404
        finally:
            ada.close()
            bob.close()
    finally:
        admin.close()


def test_the_http_layer_rejects_every_escape_attempt(team_server):
    admin = login(team_server, "admin", TEAM_ADMIN_PASSWORD)
    try:
        member = _member(team_server, admin, "ada")
        try:
            # A file the server process can read, but which is not in the workspace.
            outside = team_server.data_root / "template" / "models.json"
            assert outside.is_file()

            for hostile in ("/../../template/models.json", "..", "/./../.."):
                response = member.get("/api/workspace/file", params={"path": hostile})
                assert response.status_code == 400, (hostile, response.text)

            # Percent-encoded traversal must not be decoded twice into a real path: the
            # URL is built by hand because httpx would re-encode the `%` for us.
            encoded = member.get("/api/workspace/file?path=/..%2f..%2ftemplate%2fmodels.json")
            assert encoded.status_code == 400, encoded.text

            # Deleting the root would take the user's own workspace with it.
            assert member.delete("/api/workspace/file", params={"path": "/"}).status_code == 400
        finally:
            member.close()
    finally:
        admin.close()


def test_a_symlink_planted_by_the_agent_cannot_escape(team_server):
    """The agent can create files; a symlink it creates must not become a door."""
    admin = login(team_server, "admin", TEAM_ADMIN_PASSWORD)
    try:
        member = _member(team_server, admin, "ada")
        try:
            uid = member.get("/api/auth/me").json()["user"]["uid"]
            member.get("/api/workspace/tree")  # materialises the workspace directory
            workspace = team_server.workspace(uid)
            outside = team_server.data_root / "template"
            os.symlink(outside, workspace / "escape")

            response = member.get("/api/workspace/file", params={"path": "/escape/models.json"})
            assert response.status_code == 400, response.text
            assert json.loads(response.text)["error"].startswith("path must not leave")
        finally:
            member.close()
    finally:
        admin.close()
