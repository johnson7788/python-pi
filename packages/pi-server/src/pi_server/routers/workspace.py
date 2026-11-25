"""Workspace files, artifacts and the session transcript endpoints.

Split from ``app.py``.  Everything here answers "what is in this user's workspace":
the workspace list, file read/write/mkdir/download, artifact listing and previews,
Office/PPTX/XLSX export, and the session list / state snapshot the browser syncs from.

``workspace_for`` travels with them: it is the single place a request is turned into
a workspace root, and every path rule below depends on it.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import uuid
import zipfile
from typing import Any, Callable
from xml.etree import ElementTree

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, Response
from pi_coding_agent.session_manager import SessionManager
from pi_server import auth
from pi_server.constants import LOCAL_UID
from pi_server.db import DuplicateWorkspace
from pi_server.docx_export import markdown_to_docx
from pi_server.errors import error
from pi_server.schemas import (
    CreateDirectoryRequest,
    CreateWorkspaceRequest,
    ExportDocxRequest,
    RenameWorkspaceRequest,
)
from pi_server.workspace import Workspace


def register(
    app: FastAPI,
    *,
    database: Any,
    options: Any,
    server_config: Any,
    registry: Any,
    require_team_mode: Callable[[], Any],
    resolve_workspace: Callable[[str, str | None], str],
    workspace_row: Callable[[str, str], Any],
    ensure_default_workspace: Callable[[str], None],
    worker_for: Callable[[Request], Any],
) -> None:
    @app.get("/api/workspaces")
    async def list_workspaces(request: Request) -> dict[str, Any]:
        if database is None:
            return {
                "workspaces": [
                    {"id": "default", "name": "默认工作空间", "isDefault": True, "path": options.cwd}
                ],
                "canCreate": False,
            }
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        ensure_default_workspace(user.uid)
        rows = database.list_workspaces(user.uid)
        return {
            "workspaces": [
                {
                    "id": str(row["id"]),
                    "name": str(row["name"]),
                    "isDefault": bool(row["is_default"]),
                    "path": str(row["path"]),
                }
                for row in rows
            ],
            "canCreate": True,
        }

    @app.post("/api/workspaces", status_code=201)
    async def create_workspace(payload: CreateWorkspaceRequest, request: Request) -> dict[str, Any]:
        db = require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        name = payload.name.strip()
        if not 1 <= len(name) <= 40:
            raise error(422, "workspace name must be 1-40 characters")
        ensure_default_workspace(user.uid)
        workspace_id = uuid.uuid4().hex[:12]
        path = server_config.ensure_workspace_path(user.uid, workspace_id)
        try:
            db.create_workspace(user.uid, name, path, workspace_id)
        except DuplicateWorkspace:
            raise error(409, f"workspace already exists: {name}") from None
        return {"id": workspace_id, "name": name, "isDefault": False}

    @app.patch("/api/workspaces/{workspace_id}")
    async def rename_workspace(
        workspace_id: str, payload: RenameWorkspaceRequest, request: Request
    ) -> dict[str, Any]:
        db = require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        ensure_default_workspace(user.uid)
        if db.get_workspace(user.uid, workspace_id) is None:
            raise error(404, f"unknown workspace: {workspace_id}")
        name = payload.name.strip()
        if not 1 <= len(name) <= 40:
            raise error(422, "workspace name must be 1-40 characters")
        try:
            db.rename_workspace(user.uid, workspace_id, name)
        except DuplicateWorkspace:
            raise error(409, f"workspace already exists: {name}") from None
        return {"id": workspace_id, "name": name, "isDefault": workspace_id == "default"}

    @app.get("/api/sessions")
    async def sessions(request: Request) -> dict[str, Any]:
        worker = await worker_for(request)
        # Sessions written before apps existed have no app; keep them visible everywhere
        # instead of hiding half of someone's history.
        wanted_app = request.query_params.get("app") or ""
        wanted_mode = request.query_params.get("mode") or ""

        # Running state is per user, not per request: a conversation left mid-turn
        # keeps streaming in a worker that may belong to another workspace, and the
        # sidebar lists every workspace at once. Union the live workers.
        uid = LOCAL_UID
        if database is not None:
            user = auth.current_user(request)
            if user is None:
                raise auth.reject_unauthenticated()
            uid = user.uid
        running_files: set[str] = set()
        for live in registry.workers_for(uid):
            running_files |= live.running_session_files()

        def serialize(info: Any, workspace: dict[str, str] | None) -> dict[str, Any]:
            return {
                "path": info.path,
                "id": info.id,
                "cwd": info.cwd,
                "name": info.name,
                "app": info.app,
                "mode": info.mode,
                "modified": info.modified.isoformat(),
                "messageCount": info.message_count,
                "firstMessage": info.first_message,
                "workspace": workspace,
                "running": os.path.realpath(str(info.path)) in running_files,
            }

        # ``scope=all`` feeds the sidebar's "按工作区" grouping: one call for every
        # workspace this user owns instead of one call per switch. The session dir is
        # derived from (cwd, agent_dir), so each workspace has to be listed separately.
        if (request.query_params.get("scope") or "") != "all":
            # `agent_dir` is what keeps this list per-user: the same call in single mode
            # falls back to the default agent dir because the option is None there.
            listed = await SessionManager.list(
                worker.state.cwd,
                agent_dir=worker.options.agent_dir,
                session_dir=worker.options.session_dir,
            )
            rows = [(info, None) for info in listed]
        elif database is None:
            listed = await SessionManager.list(
                worker.state.cwd,
                agent_dir=worker.options.agent_dir,
                session_dir=worker.options.session_dir,
            )
            rows = [(info, {"id": "default", "name": "默认工作空间"}) for info in listed]
        else:
            user = auth.current_user(request)
            if user is None:
                raise auth.reject_unauthenticated()
            ensure_default_workspace(user.uid)
            rows = []
            for workspace in database.list_workspaces(user.uid):
                listed = await SessionManager.list(
                    str(workspace["path"]),
                    agent_dir=worker.options.agent_dir,
                    session_dir=worker.options.session_dir,
                )
                tag = {"id": str(workspace["id"]), "name": str(workspace["name"])}
                rows.extend((info, tag) for info in listed)

        if wanted_app:
            rows = [(info, tag) for info, tag in rows if info.app in (None, wanted_app)]
        if wanted_mode:
            rows = [(info, tag) for info, tag in rows if info.mode in (None, wanted_mode)]
        rows.sort(key=lambda row: row[0].modified, reverse=True)
        return {"sessions": [serialize(info, tag) for info, tag in rows]}

    @app.get("/api/state")
    async def state(request: Request) -> dict[str, Any]:
        worker = await worker_for(request)
        return worker.state_payload()

    _ARTIFACT_EDITABLE = {
        "md", "mdx", "txt", "html", "htm", "csv", "tsv", "json", "yaml", "yml",
    }
    _ARTIFACT_EXTENSIONS = {
        "md": "document",
        "mdx": "document",
        "docx": "document",
        "txt": "document",
        "html": "document",
        "htm": "document",
        "csv": "sheet",
        "tsv": "sheet",
        "xlsx": "sheet",
        "xls": "sheet",
        "pptx": "slides",
        "ppt": "slides",
        "pdf": "document",
        "json": "data",
        "yaml": "data",
        "yml": "data",
        "png": "image",
        "jpg": "image",
        "jpeg": "image",
        "webp": "image",
        "svg": "image",
    }

    # -- workspace files ---------------------------------------------------------------

    def workspace_for(request: Request) -> Workspace:
        """The caller's workspace.

        Deliberately does **not** start an agent session: browsing files should not cost a
        bootstrap, and it must keep working while a session is being evicted.
        """
        if database is None:
            resolve_workspace(LOCAL_UID, request.query_params.get("workspace"))
            return Workspace(options.cwd, create=False)
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        workspace = resolve_workspace(user.uid, request.query_params.get("workspace"))
        row = workspace_row(user.uid, workspace)
        return Workspace(str(row["path"]))

    @app.get("/api/artifacts")
    async def list_artifacts(request: Request) -> dict[str, Any]:
        """Recent workspace files that look like deliverables, newest first."""
        workspace = workspace_for(request)
        tree = workspace.tree("/", recursive=True, files_only=True)
        artifacts: list[dict[str, Any]] = []
        for entry in tree["entries"]:
            if entry.get("hidden"):
                continue
            extension = str(entry.get("name", "")).rsplit(".", 1)[-1].lower()
            kind = _ARTIFACT_EXTENSIONS.get(extension)
            if kind is None:
                continue
            artifacts.append(
                {
                    "path": entry["path"],
                    "name": entry["name"],
                    "kind": kind,
                    "extension": extension,
                    "size": entry["size"],
                    "modified": entry["modified"],
                    "editable": extension in _ARTIFACT_EDITABLE,
                }
            )
        artifacts.sort(key=lambda item: float(item["modified"] or 0), reverse=True)
        return {"artifacts": artifacts[:40]}

    @app.get("/api/artifacts/pptx")
    async def preview_pptx(request: Request, path: str = Query(...)) -> dict[str, Any]:
        """Read-only slide text preview, extracted from the OOXML package.

        Deliberately no python-pptx dependency: the package is a zip of XML, and the
        preview only needs the text runs.  Editing stays a download-and-open workflow.
        """
        workspace = workspace_for(request)
        target, _name = workspace.download_target(path)
        try:
            with zipfile.ZipFile(target) as archive:
                names = [
                    name
                    for name in archive.namelist()
                    if re.fullmatch(r"ppt/slides/slide[0-9]+\.xml", name)
                ]
                names.sort(key=lambda name: int(re.search(r"slide([0-9]+)\.xml$", name).group(1)))
                slides: list[dict[str, Any]] = []
                for index, name in enumerate(names[:100], start=1):
                    root = ElementTree.fromstring(archive.read(name))
                    texts = [
                        node.text
                        for node in root.iter()
                        if node.tag.endswith("}t") and node.text and node.text.strip()
                    ]
                    slides.append({"number": index, "texts": texts[:80]})
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as failure:
            raise error(422, f"not a readable pptx: {failure}") from None
        return {"path": path, "slides": slides}

    @app.get("/api/artifacts/xlsx")
    async def preview_xlsx(request: Request, path: str = Query(...)) -> dict[str, Any]:
        """Read-only XLSX preview: sheet names and the first cells of each sheet."""
        workspace = workspace_for(request)
        target, _name = workspace.download_target(path)

        def local(tag: str) -> str:
            return tag.rsplit("}", 1)[-1]

        def column_index(reference: str) -> int:
            letters = "".join(char for char in reference if char.isalpha()).upper()
            index = 0
            for char in letters:
                index = index * 26 + (ord(char) - 64)
            return max(0, index - 1)

        try:
            with zipfile.ZipFile(target) as archive:
                names = set(archive.namelist())
                shared: list[str] = []
                if "xl/sharedStrings.xml" in names:
                    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                    for item in root.iter():
                        if local(item.tag) == "si":
                            shared.append("".join(node.text or "" for node in item.iter() if local(node.tag) == "t"))

                relations: dict[str, str] = {}
                if "xl/_rels/workbook.xml.rels" in names:
                    root = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
                    for item in root.iter():
                        if local(item.tag) == "Relationship":
                            relations[str(item.attrib.get("Id") or "")] = str(item.attrib.get("Target") or "")

                workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
                sheets: list[dict[str, Any]] = []
                for item in workbook.iter():
                    if local(item.tag) != "sheet":
                        continue
                    rel_id = next(
                        (value for key, value in item.attrib.items() if local(key) == "id"), ""
                    )
                    target_name = relations.get(rel_id, "")
                    if not target_name:
                        continue
                    sheet_path = target_name.lstrip("/")
                    if not sheet_path.startswith("xl/"):
                        sheet_path = f"xl/{sheet_path}"
                    if sheet_path not in names:
                        continue
                    sheet_root = ElementTree.fromstring(archive.read(sheet_path))
                    rows: list[list[str]] = []
                    for row in sheet_root.iter():
                        if local(row.tag) != "row":
                            continue
                        cells: dict[int, str] = {}
                        for cell in row:
                            if local(cell.tag) != "c":
                                continue
                            reference = str(cell.attrib.get("r") or "")
                            cell_type = str(cell.attrib.get("t") or "")
                            value = ""
                            if cell_type == "inlineStr":
                                value = "".join(
                                    node.text or "" for node in cell.iter() if local(node.tag) == "t"
                                )
                            else:
                                raw = next(
                                    (node.text or "" for node in cell.iter() if local(node.tag) == "v"),
                                    "",
                                )
                                if cell_type == "s" and raw.isdigit():
                                    index = int(raw)
                                    value = shared[index] if 0 <= index < len(shared) else ""
                                elif cell_type == "b":
                                    value = "TRUE" if raw == "1" else "FALSE"
                                else:
                                    value = raw
                            if value:
                                cells[column_index(reference)] = value
                        if cells:
                            width = min(max(cells) + 1, 30)
                            rows.append([cells.get(index, "") for index in range(width)])
                        if len(rows) >= 200:
                            break
                    sheets.append({"name": str(item.attrib.get("name") or sheet_path), "rows": rows})
                    if len(sheets) >= 8:
                        break
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as failure:
            raise error(422, f"not a readable xlsx: {failure}") from None
        return {"path": path, "sheets": sheets}

    @app.post("/api/export/docx")
    async def export_docx(payload: ExportDocxRequest, request: Request) -> Response:
        """把一条回答（Markdown）转成 Word，供"下载"按钮使用。

        只做格式转换，不碰工作区：内容来自浏览器里已有的那条回答。
        """
        if database is not None and auth.current_user(request) is None:
            raise auth.reject_unauthenticated()
        text = payload.markdown or ""
        if not text.strip():
            raise error(422, "markdown is empty")
        if len(text) > 400_000:
            raise error(413, "markdown is too large")
        data = markdown_to_docx(
            text, title=payload.title or "回答", subtitle=payload.subtitle or ""
        )
        name = (payload.title or "answer").strip()[:60] or "answer"
        # 注意 str.isalnum() 对中文也是 True，header 必须是 ASCII，所以额外要求 isascii()。
        safe = "".join(
            ch if ch.isascii() and (ch.isalnum() or ch in "-_ ") else "-" for ch in name
        ).strip(" -") or "answer"
        # HTTP 头只能是 latin-1：中文文件名走 RFC 5987 的 filename*，另给一个 ASCII 回退。
        encoded = urllib.parse.quote(f"{name}.docx")
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": f"attachment; filename=\"{safe}.docx\"; filename*=UTF-8''{encoded}"
            },
        )

    @app.get("/api/workspace/tree")
    async def workspace_tree(
        request: Request,
        path: str = Query("/", description="Workspace-relative directory"),
        recursive: bool = Query(False),
        files_only: bool = Query(False),
    ) -> dict[str, Any]:
        return workspace_for(request).tree(path, recursive=recursive, files_only=files_only)

    @app.get("/api/workspace/file")
    async def workspace_read(request: Request, path: str = Query(...)) -> dict[str, Any]:
        return workspace_for(request).read(path)

    @app.put("/api/workspace/file")
    async def workspace_write(request: Request, path: str = Query(...)) -> dict[str, Any]:
        """Write a file from either JSON (text edits) or raw bytes (uploads).

        One endpoint serves both so that a binary upload needs no multipart parser, and so
        the size cap and the path rules are applied exactly once.
        """
        body = await request.body()
        media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if media_type in ("application/json", "text/json"):
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError as decode_error:
                raise error(422, f"invalid JSON body: {decode_error}") from None
            content = payload.get("content") if isinstance(payload, dict) else None
            if not isinstance(content, str):
                raise error(422, "JSON body must be {\"content\": \"...\"}")
            data = content.encode("utf-8")
        else:
            data = body
        return workspace_for(request).write(path, data)

    @app.delete("/api/workspace/file")
    async def workspace_delete(request: Request, path: str = Query(...)) -> dict[str, Any]:
        return workspace_for(request).delete(path)

    @app.post("/api/workspace/directory")
    async def workspace_mkdir(
        request: Request, payload: CreateDirectoryRequest
    ) -> dict[str, Any]:
        return workspace_for(request).create_directory(payload.parentPath, payload.name)

    @app.get("/api/workspace/download")
    async def workspace_download(
        request: Request, path: str = Query(...), inline: bool = Query(False)
    ) -> FileResponse:
        target, filename = workspace_for(request).download_target(path)
        return FileResponse(
            target,
            filename=filename,
            content_disposition_type="inline" if inline else "attachment",
        )
