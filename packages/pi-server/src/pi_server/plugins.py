"""Installed python-pi plugins: manifest discovery and entry paths.

A plugin is a directory with ``plugin.json`` and a Python entry module exposing
``setup(api)``.  It is executed in-process, like an extension, so this catalog only
decides *which* plugins are eligible; enablement is per tenant/app and enforced when a
worker is created.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,31}$")
_SUBMISSION_ID = re.compile(r"^[a-f0-9]{12}$")

#: Written next to a submitted package; the package itself lives in ``plugin/``.
SUBMISSION_META = "submission.json"

#: Capability names a manifest may declare.  Unknown names are rejected rather than
#: ignored: a typo ("networking") would otherwise silently grant nothing while the
#: author believes the plugin asked for the right thing.
KNOWN_CAPABILITIES = frozenset({"tools", "network", "files", "process", "secrets"})

#: Permissions whose presence implies a capability.  ``permissions`` is the detailed
#: form and ``capabilities`` the summary; a manifest whose two halves disagree is a
#: manifest nobody can reason about, so it is refused.
_PERMISSION_CAPABILITIES = {
    "network": "network",
    "filesystem": "files",
    "process": "process",
    "secrets": "secrets",
}


def _declared_permissions(data: dict[str, Any]) -> dict[str, Any]:
    raw = data.get("permissions")
    return raw if isinstance(raw, dict) else {}


def _permission_capabilities(permissions: dict[str, Any]) -> set[str]:
    """Capabilities that ``permissions`` actually exercises."""
    required: set[str] = set()
    network = permissions.get("network")
    if network is True or (isinstance(network, list) and network):
        required.add("network")
    filesystem = str(permissions.get("filesystem") or "none").lower()
    if filesystem in ("read", "write"):
        required.add("files")
    if permissions.get("process") is True:
        required.add("process")
    secrets = permissions.get("secrets")
    if isinstance(secrets, list) and secrets:
        required.add("secrets")
    return required


def _network_hosts(permissions: dict[str, Any]) -> tuple[str, ...]:
    """Hosts the manifest says it talks to (``true`` means "anywhere")."""
    network = permissions.get("network")
    if isinstance(network, list):
        return tuple(str(host).strip() for host in network if str(host).strip())
    return ()


@dataclass(frozen=True)
class PluginInfo:
    id: str
    name: str
    version: str
    description: str
    entry: str
    capabilities: tuple[str, ...]
    path: str
    #: Declared host allowlist from ``permissions.network`` (empty = none declared).
    network_hosts: tuple[str, ...] = ()

    @property
    def entry_path(self) -> str:
        return os.path.join(self.path, self.entry)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "capabilities": list(self.capabilities),
            # Shown to an administrator rather than enforced: an in-process plugin
            # can reach anything the server can.  Publishing the claim at least makes
            # approval an informed decision.
            "networkHosts": list(self.network_hosts),
        }


def _load_manifest(path: str) -> PluginInfo | None:
    manifest_path = os.path.join(path, "plugin.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    plugin_id = str(data.get("id") or "").strip()
    entry = str(data.get("entry") or "main.py").strip()
    if not _ID.match(plugin_id):
        return None
    if not entry or entry.startswith(("/", "..")) or ".." in entry.split("/"):
        return None
    entry_file = os.path.join(path, entry)
    if not os.path.isfile(entry_file):
        return None
    # A manifest is supplied by whoever ships the plugin, so a *symlinked* entry is
    # a way to run code from outside the plugin directory while the path looks
    # local.  Compare real paths: the entry must resolve inside the plugin root.
    root = os.path.realpath(path)
    resolved_entry = os.path.realpath(entry_file)
    if os.path.commonpath([root, resolved_entry]) != root:
        return None
    version = str(data.get("version") or "0.0.0").strip()
    if not _VERSION.match(version):
        return None
    capabilities = data.get("capabilities")
    caps = tuple(str(item) for item in capabilities) if isinstance(capabilities, list) else ()
    if any(cap not in KNOWN_CAPABILITIES for cap in caps):
        return None
    permissions = _declared_permissions(data)
    # The two halves of the manifest must agree.  Without this, a plugin can ship
    # ``permissions.network`` while advertising only ``tools``, and the approval UI
    # (which reads capabilities) under-reports what it does.
    if not _permission_capabilities(permissions).issubset(caps):
        return None
    return PluginInfo(
        id=plugin_id,
        name=str(data.get("name") or plugin_id),
        version=version,
        description=str(data.get("description") or ""),
        entry=entry,
        capabilities=caps,
        path=os.path.abspath(path),
        network_hosts=_network_hosts(permissions),
    )


def scan(root: str) -> list[PluginInfo]:
    """Plugins under ``root``, one directory per plugin; broken ones are skipped."""
    if not os.path.isdir(root):
        return []
    found: list[PluginInfo] = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        info = _load_manifest(path)
        if info is not None:
            found.append(info)
    return found


def entry_paths(root: str, plugin_ids: Iterable[str]) -> list[str]:
    wanted = {str(plugin_id) for plugin_id in plugin_ids}
    return [info.entry_path for info in scan(root) if info.id in wanted]


# --------------------------------------------------------------------------------------
# Submissions: an author uploads, an administrator reviews, nothing is enabled yet.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SubmissionInfo:
    sid: str
    plugin: PluginInfo
    submitter: str
    created_at: float

    @property
    def base(self) -> str:
        """The submission directory (``plugin/`` plus ``submission.json``)."""
        return os.path.dirname(self.plugin.path)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.sid,
            "pluginId": self.plugin.id,
            "name": self.plugin.name,
            "version": self.plugin.version,
            "description": self.plugin.description,
            "submitter": self.submitter,
            "createdAt": self.created_at,
        }


def submissions_root(plugin_root: str) -> str:
    """Sibling of the plugin root, so ``data_root`` holds both."""
    return os.path.join(os.path.dirname(os.path.abspath(plugin_root)), "plugin-submissions")


def scan_submissions(root: str) -> list[SubmissionInfo]:
    if not os.path.isdir(root):
        return []
    found: list[SubmissionInfo] = []
    for sid in sorted(os.listdir(root)):
        if not _SUBMISSION_ID.match(sid):
            continue
        base = os.path.join(root, sid)
        try:
            with open(os.path.join(base, SUBMISSION_META), "r", encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        plugin = _load_manifest(os.path.join(base, "plugin"))
        if plugin is None or not isinstance(meta, dict):
            continue
        found.append(
            SubmissionInfo(
                sid=sid,
                plugin=plugin,
                submitter=str(meta.get("submitter") or ""),
                created_at=float(meta.get("createdAt") or 0.0),
            )
        )
    return found


def find_submission(root: str, sid: str) -> SubmissionInfo | None:
    if not _SUBMISSION_ID.match(sid):
        return None
    return next((item for item in scan_submissions(root) if item.sid == sid), None)
