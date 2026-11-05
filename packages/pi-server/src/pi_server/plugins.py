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


@dataclass(frozen=True)
class PluginInfo:
    id: str
    name: str
    version: str
    description: str
    entry: str
    capabilities: tuple[str, ...]
    path: str

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
    if not os.path.isfile(os.path.join(path, entry)):
        return None
    version = str(data.get("version") or "0.0.0").strip()
    if not _VERSION.match(version):
        return None
    capabilities = data.get("capabilities")
    caps = tuple(str(item) for item in capabilities) if isinstance(capabilities, list) else ()
    return PluginInfo(
        id=plugin_id,
        name=str(data.get("name") or plugin_id),
        version=version,
        description=str(data.get("description") or ""),
        entry=entry,
        capabilities=caps,
        path=os.path.abspath(path),
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
