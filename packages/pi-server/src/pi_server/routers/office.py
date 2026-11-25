"""Office documents and the spreadsheet sidecar.

Split from ``app.py``.  The document-address and sheet-session helpers travel with
the routes that use them, because they encode the same rules: the id names a path,
the path must resolve inside the caller's workspace, and a sheet session belongs to
one user.

The sidecar session map is created per application, not at module scope: tests build
several apps in one process, and a leaked session would point at a deleted directory.
"""

from __future__ import annotations

import base64
import os
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pi_server import auth, office as office_store
from pi_server import xlsx_sidecar as xlsx_engine
from pi_server.constants import LOCAL_UID
from pi_server.errors import error
from pi_server.workspace import Workspace


def register(
    app: FastAPI,
    *,
    database: Any,
    options: Any,
    resolve_workspace: Callable[[str, str | None], str],
    workspace_row: Callable[[str, str], Any],
) -> None:
    #: sidecar sessionId -> {sessionId, uid, dir, path}; per application, see module docstring.
    sheets_sessions: dict[str, dict[str, Any]] = {}

    def office_target(request: Request, doc_id: str, *, must_exist: bool = True) -> tuple[str, Any, str]:
        try:
            ref = office_store.decode_doc_id(doc_id)
        except office_store.OfficeIdError as failure:
            raise error(400, str(failure)) from None
        if database is None:
            resolve_workspace(LOCAL_UID, ref.workspace)
            root = options.cwd
        else:
            user = auth.current_user(request)
            if user is None:
                raise auth.reject_unauthenticated()
            workspace = resolve_workspace(user.uid, ref.workspace)
            root = str(workspace_row(user.uid, workspace)["path"])
        workspace_obj = Workspace(root, create=False)
        try:
            path = workspace_obj.path_for(ref.path, must_exist=must_exist)
        except Exception as failure:  # noqa: BLE001 - workspace errors are handled by the app
            raise error(404, str(failure)) from None
        return path, ref, root

    @app.get("/api/office/documents/{doc_id}")
    async def office_metadata(doc_id: str, request: Request) -> dict[str, Any]:
        path, ref, _root = office_target(request, doc_id)
        stat = os.stat(path)
        return {
            "id": doc_id,
            "name": os.path.basename(path),
            "path": ref.path,
            "size": stat.st_size,
            "modified": stat.st_mtime,
            "sha": office_store.sha256_file(path),
        }

    @app.get("/api/office/documents/{doc_id}/blob")
    async def office_blob(doc_id: str, request: Request) -> Response:
        path, _ref, _root = office_target(request, doc_id)
        with open(path, "rb") as handle:
            data = handle.read()
        return Response(content=data, media_type="application/octet-stream")

    @app.put("/api/office/documents/{doc_id}/blob")
    async def office_save(doc_id: str, request: Request) -> dict[str, Any]:
        path, ref, root = office_target(request, doc_id, must_exist=False)
        if database is not None:
            lock = database.office_lock(doc_id)
            (request.headers.get("x-office-token") or "").strip()
            if lock is not None and float(lock["expires_at"]) > time.time():
                user = auth.current_user(request)
                if user is None or lock["uid"] != user.uid:
                    raise error(409, "document is locked")
        data = await request.body()
        if not data or len(data) > office_store.MAX_OFFICE_BYTES:
            raise error(413, "office file must be 1 byte - 50 MB")
        if os.path.exists(path):
            current = office_store.sha256_file(path)
            expected = (request.headers.get("if-match") or "").strip().strip('"')
            if expected and expected != current:
                raise error(409, "document changed on disk")
            office_store.backup_version(root, doc_id, path)
        office_store.atomic_write(path, data)
        return {
            "id": doc_id,
            "path": ref.path,
            "size": len(data),
            "sha": office_store.sha256_file(path),
        }

    @app.post("/api/office/documents/{doc_id}/lock")
    async def office_lock(doc_id: str, request: Request) -> dict[str, Any]:
        path, _ref, _root = office_target(request, doc_id)
        if database is None:
            return {"token": "single", "expiresAt": time.time() + 3600, "owner": "single"}
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        token = secrets.token_urlsafe(24)
        row = database.acquire_office_lock(doc_id, user.uid, token, ttl_seconds=120)
        if row["uid"] != user.uid:
            raise error(409, "document is being edited by another user", details={"owner": row["uid"]})
        return {"token": token, "expiresAt": float(row["expires_at"]), "owner": user.uid}

    @app.delete("/api/office/documents/{doc_id}/lock")
    async def office_unlock(doc_id: str, request: Request) -> dict[str, Any]:
        office_target(request, doc_id, must_exist=False)
        if database is None:
            return {"released": True}
        token = (request.headers.get("x-office-token") or "").strip()
        return {"released": database.release_office_lock(doc_id, token)}

    @app.get("/api/office/documents/{doc_id}/versions")
    async def office_versions(doc_id: str, request: Request) -> dict[str, Any]:
        _path, _ref, root = office_target(request, doc_id)
        directory = Path(root) / ".pi" / "versions" / doc_id
        versions: list[dict[str, Any]] = []
        if directory.is_dir():
            for entry in sorted(directory.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True):
                if entry.is_file():
                    versions.append(
                        {
                            "id": entry.name,
                            "size": entry.stat().st_size,
                            "created": entry.stat().st_mtime,
                        }
                    )
        return {"versions": versions}

    @app.post("/api/office/documents/{doc_id}/versions/{version}/restore")
    async def office_restore(doc_id: str, version: str, request: Request) -> dict[str, Any]:
        path, ref, root = office_target(request, doc_id)
        if database is not None:
            lock = database.office_lock(doc_id)
            (request.headers.get("x-office-token") or "").strip()
            if lock is not None and float(lock["expires_at"]) > time.time():
                user = auth.current_user(request)
                if user is None or lock["uid"] != user.uid:
                    raise error(409, "document is locked")
        directory = (Path(root) / ".pi" / "versions" / doc_id).resolve()
        candidate = (directory / version).resolve()
        if candidate.parent != directory or not candidate.is_file():
            raise error(404, "version not found")
        office_store.backup_version(root, doc_id, path)
        office_store.atomic_write(path, candidate.read_bytes())
        return {
            "id": doc_id,
            "path": ref.path,
            "sha": office_store.sha256_file(path),
            "size": candidate.stat().st_size,
        }

    # -- xlsx sidecar sessions --------------------------------------------------------

    def sheet_uid(request: Request) -> str:
        if database is None:
            return LOCAL_UID
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        return user.uid

    def sheet_session(request: Request, session_id: str) -> dict[str, Any]:
        session = sheets_sessions.get(session_id)
        if session is None or session["uid"] != sheet_uid(request):
            raise error(404, "workbook session not found")
        return session

    async def sheet_call(command: str, **fields: Any) -> Any:
        try:
            return await xlsx_engine.sidecar().request(command, **fields)
        except xlsx_engine.SidecarError as failure:
            raise error(503, f"xlsx engine: {failure}") from None

    @app.post("/api/office/sheets/open")
    async def sheets_open(request: Request) -> dict[str, Any]:
        data = await request.body()
        if not data or len(data) > office_store.MAX_OFFICE_BYTES:
            raise error(413, "workbook is empty or too large")
        work_dir = Path(tempfile.mkdtemp(prefix="pi-xlsx-"))
        path = work_dir / "workbook.xlsx"
        path.write_bytes(data)
        result = await sheet_call("open", path=str(path))
        sheets_sessions[result["sessionId"]] = {
            "sessionId": result["sessionId"],
            "uid": sheet_uid(request),
            "dir": work_dir,
            "path": path,
        }
        return result

    @app.post("/api/office/sheets/read-range")
    async def sheets_read_range(request: Request) -> Any:
        body = await request.json()
        session = sheet_session(request, str(body.get("sessionId") or ""))
        return await sheet_call(
            "read_range",
            sessionId=session["sessionId"],
            sheetId=body.get("sheetId"),
            range=body.get("range"),
        )

    @app.post("/api/office/sheets/read-formulas")
    async def sheets_read_formulas(request: Request) -> Any:
        body = await request.json()
        session = sheet_session(request, str(body.get("sessionId") or ""))
        return await sheet_call(
            "read_formula_cells", sessionId=session["sessionId"], sheetId=body.get("sheetId")
        )

    @app.post("/api/office/sheets/read-media")
    async def sheets_read_media(request: Request) -> Any:
        body = await request.json()
        session = sheet_session(request, str(body.get("sessionId") or ""))
        return await sheet_call(
            "read_media", sessionId=session["sessionId"], visualId=body.get("visualId")
        )

    @app.post("/api/office/sheets/recalc")
    async def sheets_recalc(request: Request) -> Any:
        body = await request.json()
        session = sheet_session(request, str(body.get("sessionId") or ""))
        return await sheet_call(
            "recalc_cells",
            path=str(session["path"]),
            edits=body.get("edits") or [],
            reads=body.get("reads") or [],
        )

    @app.post("/api/office/sheets/manifest")
    async def sheets_manifest(request: Request) -> Any:
        body = await request.json()
        session = sheet_session(request, str(body.get("sessionId") or ""))
        return await sheet_call("archive_manifest", path=str(session["path"]))

    @app.post("/api/office/sheets/read-entries")
    async def sheets_read_entries(request: Request) -> Any:
        body = await request.json()
        session = sheet_session(request, str(body.get("sessionId") or ""))
        entries = body.get("entries") or []
        if not entries:
            return {"entries": []}
        out_dir = Path(tempfile.mkdtemp(prefix="pi-xlsx-read-", dir=session["dir"]))
        try:
            result = await sheet_call(
                "read_entries",
                path=str(session["path"]),
                entries=entries,
                outputDir=str(out_dir),
            )
            return {
                "entries": [
                    {
                        "name": entry["name"],
                        "contentB64": base64.b64encode(Path(entry["path"]).read_bytes()).decode(),
                    }
                    for entry in result.get("entries", [])
                ]
            }
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    @app.post("/api/office/sheets/scan-entries")
    async def sheets_scan_entries(request: Request) -> Any:
        body = await request.json()
        session = sheet_session(request, str(body.get("sessionId") or ""))
        return await sheet_call(
            "scan_entries",
            path=str(session["path"]),
            entries=body.get("entries") or [],
            needle=body.get("needle") or "",
        )

    @app.post("/api/office/sheets/save")
    async def sheets_save(request: Request) -> dict[str, Any]:
        body = await request.json()
        session = sheet_session(request, str(body.get("sessionId") or ""))
        doc_id = str(body.get("docId") or "")
        if not doc_id:
            raise error(422, "save requires an existing document")
        path, _ref, root = office_target(request, doc_id, must_exist=True)
        if database is not None:
            lock = database.office_lock(doc_id)
            if lock is not None and float(lock["expires_at"]) > time.time() and lock["uid"] != sheet_uid(request):
                raise error(409, "document is locked")

        def materialize(items: list[dict[str, Any]], tag: str) -> list[dict[str, str]]:
            output: list[dict[str, str]] = []
            for index, item in enumerate(items):
                target = Path(session["dir"]) / f"{tag}-{index}.bin"
                target.write_bytes(base64.b64decode(item.get("contentB64") or ""))
                output.append({"name": str(item.get("name") or ""), "contentPath": str(target)})
            return output

        replacements = materialize(body.get("replacements") or [], "replace")
        additions = materialize(body.get("additions") or [], "add")
        target = Path(session["dir"]) / "saved.xlsx"
        await sheet_call(
            "save_archive",
            sourcePath=str(session["path"]),
            targetPath=str(target),
            replacements=replacements,
            removals=body.get("removals") or [],
            additions=additions,
        )
        data = target.read_bytes()
        office_store.backup_version(root, doc_id, path)
        office_store.atomic_write(path, data)

        # Reset the sidecar session on the saved file, like AIOffice.
        await sheet_call("close", sessionId=session["sessionId"])
        reopened = await sheet_call("open", path=str(target))
        sheets_sessions.pop(session["sessionId"], None)
        sheets_sessions[reopened["sessionId"]] = {
            "sessionId": reopened["sessionId"],
            "uid": sheet_uid(request),
            "dir": session["dir"],
            "path": target,
        }
        return {
            "docId": doc_id,
            "size": len(data),
            "sha": office_store.sha256_file(path),
            "workbook": reopened,
        }

    @app.post("/api/office/sheets/close")
    async def sheets_close(request: Request) -> dict[str, Any]:
        body = await request.json()
        session = sheets_sessions.pop(str(body.get("sessionId") or ""), None)
        if session is not None:
            try:
                await sheet_call("close", sessionId=body.get("sessionId"))
            except HTTPException:
                pass
            shutil.rmtree(session["dir"], ignore_errors=True)
        return {"closed": True}
