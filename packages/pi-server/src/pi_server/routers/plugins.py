"""Plugin installation, submissions and per-app enablement.

Split from ``app.py``.  Installing a plugin is a trust decision, so this module owns
the two places that decision is made: the zip extractor (which must not write outside
the staging directory) and the admin-only approval of a submission.

A submission is never enabled by being approved — the approver decides *what* is
installed, an operator still decides *where* it runs.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import time
import uuid
import zipfile
from typing import Any, Callable

from fastapi import FastAPI, Query, Request
from pi_server import apps as app_catalog
from pi_server import auth
from pi_server import plugins as plugin_catalog
from pi_server.errors import error
from pi_server.schemas import PluginToggleRequest


def register(
    app: FastAPI,
    *,
    server_config: Any,
    require_team_mode: Callable[[], Any],
    plugin_paths_for: Callable[[str, str], tuple[str, ...]],
    registry: Any,
    database: Any,
    resolve_app_mode: Callable[[str | None, str | None], tuple[str, str]],
) -> None:
    def _extract_plugin_zip(data: bytes, staging: str) -> Any:
        """Validate a plugin zip and extract it (safely) into ``staging``."""
        if not data or len(data) > 10 * 1024 * 1024:
            raise error(413, "plugin zip must be 1 byte - 10 MB")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                total = 0
                for info in archive.infolist():
                    name = info.filename.replace("\\", "/")
                    if name.startswith("/") or ".." in name.split("/"):
                        raise error(422, "zip contains an unsafe path")
                    if (info.external_attr >> 16) & 0o170000 == 0o120000:
                        raise error(422, "zip contains a symlink")
                    total += int(info.file_size)
                    if total > 20 * 1024 * 1024:
                        raise error(413, "uncompressed plugin is too large")
                    target = os.path.join(staging, *name.split("/"))
                    if name.endswith("/"):
                        os.makedirs(target, exist_ok=True)
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with archive.open(info) as source, open(target, "wb") as destination:
                        shutil.copyfileobj(source, destination)
        except zipfile.BadZipFile:
            raise error(422, "not a valid zip file") from None

        found = plugin_catalog.scan(staging)
        if len(found) != 1:
            top = [item for item in os.listdir(staging) if os.path.isdir(os.path.join(staging, item))]
            if len(top) == 1:
                found = plugin_catalog.scan(os.path.join(staging, top[0]))
        if len(found) != 1:
            raise error(422, "zip must contain exactly one plugin.json")
        return found[0]

    @app.post("/api/plugins/install", status_code=201)
    async def install_plugin(request: Request) -> dict[str, Any]:
        require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        if not user.is_admin:
            raise error(403, "only administrators can install plugins")
        data = await request.body()
        root = server_config.plugin_root
        os.makedirs(root, exist_ok=True)
        staging = tempfile.mkdtemp(prefix=".install-", dir=root)
        try:
            info = _extract_plugin_zip(data, staging)
            target = os.path.join(root, info.id)
            if os.path.exists(target):
                backup = f"{target}.bak-{uuid.uuid4().hex[:6]}"
                os.rename(target, backup)
                try:
                    shutil.move(info.path, target)
                except Exception:
                    shutil.move(backup, target)
                    raise
                shutil.rmtree(backup, ignore_errors=True)
            else:
                shutil.move(info.path, target)
            return {**info.to_json(), "installed": True, "enabled": False}
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @app.post("/api/plugin-submissions", status_code=201)
    async def submit_plugin(request: Request) -> dict[str, Any]:
        """Any team member may submit; nothing is installed until an admin approves."""
        require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        data = await request.body()
        root = plugin_catalog.submissions_root(server_config.plugin_root)
        os.makedirs(root, exist_ok=True)
        sid = uuid.uuid4().hex[:12]
        staging = tempfile.mkdtemp(prefix=".submit-", dir=root)
        try:
            info = _extract_plugin_zip(data, staging)
            target = os.path.join(root, sid)
            os.makedirs(target)
            shutil.move(info.path, os.path.join(target, "plugin"))
            with open(
                os.path.join(target, plugin_catalog.SUBMISSION_META), "w", encoding="utf-8"
            ) as handle:
                json.dump(
                    {"submitter": user.username, "createdAt": time.time()},
                    handle,
                    ensure_ascii=False,
                )
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return {
            "id": sid,
            "pluginId": info.id,
            "name": info.name,
            "version": info.version,
            "submitted": True,
        }

    @app.get("/api/plugin-submissions")
    async def list_plugin_submissions(request: Request) -> dict[str, Any]:
        require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        if not user.is_admin:
            raise error(403, "only administrators can review submissions")
        root = plugin_catalog.submissions_root(server_config.plugin_root)
        return {
            "submissions": [item.to_json() for item in plugin_catalog.scan_submissions(root)]
        }

    @app.post("/api/plugin-submissions/{submission_id}/approve")
    async def approve_plugin_submission(submission_id: str, request: Request) -> dict[str, Any]:
        require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        if not user.is_admin:
            raise error(403, "only administrators can approve submissions")
        root = plugin_catalog.submissions_root(server_config.plugin_root)
        item = plugin_catalog.find_submission(root, submission_id)
        if item is None:
            raise error(404, "unknown submission")
        plugin_root = server_config.plugin_root
        os.makedirs(plugin_root, exist_ok=True)
        target = os.path.join(plugin_root, item.plugin.id)
        if os.path.exists(target):
            raise error(409, f"plugin {item.plugin.id} is already installed; uninstall it first")
        shutil.move(item.plugin.path, target)
        shutil.rmtree(item.base, ignore_errors=True)
        return {**item.plugin.to_json(), "installed": True, "enabled": False}

    @app.delete("/api/plugin-submissions/{submission_id}")
    async def reject_plugin_submission(submission_id: str, request: Request) -> dict[str, Any]:
        require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        if not user.is_admin:
            raise error(403, "only administrators can reject submissions")
        root = plugin_catalog.submissions_root(server_config.plugin_root)
        item = plugin_catalog.find_submission(root, submission_id)
        if item is None:
            raise error(404, "unknown submission")
        shutil.rmtree(item.base, ignore_errors=True)
        return {"deleted": submission_id}

    @app.delete("/api/plugins/{plugin_id}")
    async def uninstall_plugin(plugin_id: str, request: Request) -> dict[str, Any]:
        db = require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        if not user.is_admin:
            raise error(403, "only administrators can uninstall plugins")
        installed = {item.id: item for item in plugin_catalog.scan(server_config.plugin_root)}
        item = installed.get(plugin_id)
        if item is None:
            raise error(404, f"unknown plugin: {plugin_id}")
        shutil.rmtree(item.path, ignore_errors=True)
        db.delete_plugin_enable(plugin_id)
        await registry.evict_plugin(item.path)
        return {"deleted": plugin_id}

    @app.get("/api/plugins")
    async def list_plugins(request: Request, app: str = Query("general")) -> dict[str, Any]:
        installed = plugin_catalog.scan(server_config.plugin_root)
        can_install = False
        app_id, _mode_id = resolve_app_mode(app, None)
        app_def = app_catalog.find_app(app_id)
        # An app that declares a plugin gets it whether or not anyone clicked "enable", so
        # report it as enabled here too — otherwise the page says "off" for a tool the
        # model is already using.
        required = set(app_def.plugins) if app_def is not None else set()
        if database is None:
            states: dict[str, bool] = {}
            can_manage = False
        else:
            user = auth.current_user(request)
            if user is None:
                raise auth.reject_unauthenticated()
            states = database.plugin_states(user.uid, app_id)
            can_manage = True
            can_install = bool(user.is_admin)
        return {
            "app": app_id,
            "canManage": can_manage,
            "canInstall": can_install,
            "plugins": [
                {
                    **info.to_json(),
                    "enabled": states.get(info.id, False) or info.id in required,
                    "required": info.id in required,
                }
                for info in installed
            ],
        }

    @app.patch("/api/plugins/{plugin_id}/enable")
    async def toggle_plugin(
        plugin_id: str,
        body: PluginToggleRequest,
        request: Request,
        app: str = Query("general"),
    ) -> dict[str, Any]:
        db = require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        app_id, _mode_id = resolve_app_mode(body.appId or app, None)
        if all(info.id != plugin_id for info in plugin_catalog.scan(server_config.plugin_root)):
            raise error(404, f"unknown plugin: {plugin_id}")
        app_def = app_catalog.find_app(app_id)
        if not body.enabled and app_def is not None and plugin_id in app_def.plugins:
            raise error(409, f"the app declares plugin {plugin_id!r}; it cannot be switched off here")
        db.set_plugin_enabled(user.uid, app_id, plugin_id, body.enabled)
        # The next request/worker for this app reloads the plugin set; running turns are
        # allowed to finish on the old set rather than mutating tools mid-turn.
        await registry.evict(user.uid, app_id)
        return {"id": plugin_id, "enabled": body.enabled, "app": app_id}

    @app.get("/api/plugins/status")
    async def plugin_status(request: Request, app: str = Query("general")) -> dict[str, Any]:
        """What each installed plugin registered in this user's live worker.

        A plugin that failed to import never reaches a worker, so the loader's
        diagnostics are the only place the reason exists.  Without them the UI can
        only say "enabled", which is indistinguishable from "silently broken".
        """
        require_team_mode()
        user = auth.current_user(request)
        if user is None:
            raise auth.reject_unauthenticated()
        app_id, _mode_id = resolve_app_mode(app, None)

        workers = [
            worker
            for worker in registry.workers_for(user.uid)
            if worker.app_id == app_id and worker.started
        ]
        runner = workers[0].state.extension_runner if workers else None
        snapshot = runner.status() if runner is not None else {"extensions": [], "diagnostics": []}

        def _inside(raw: str, root: str) -> bool:
            try:
                return os.path.realpath(raw).startswith(root + os.sep)
            except (OSError, ValueError):
                return False

        plugins: list[dict[str, Any]] = []
        for info in plugin_catalog.scan(server_config.plugin_root):
            root = os.path.realpath(info.path)
            entries = [entry for entry in snapshot["extensions"] if _inside(entry["path"], root)]
            diagnostics = [
                item for item in snapshot["diagnostics"] if _inside(item["path"], root)
            ]
            plugins.append(
                {
                    "id": info.id,
                    "loaded": bool(entries),
                    "tools": sorted({name for entry in entries for name in entry["tools"]}),
                    "errors": [
                        item["message"] for item in diagnostics if item["severity"] == "error"
                    ],
                    "warnings": [
                        item["message"] for item in diagnostics if item["severity"] != "error"
                    ],
                }
            )
        return {"app": app_id, "runtimeLoaded": runner is not None, "plugins": plugins}
