"""A plugin manifest's two halves must agree, and the repo's own plugins must too.

``capabilities`` is the summary an approver reads; ``permissions`` is the detail the
author filled in.  Before this check the shipped plugins declared
``capabilities: ["tools"]`` while requesting network, files, process and secrets —
so the approval UI under-reported what installing them grants.

Enforcement is deliberately at *load* time and about honesty, not isolation: an
in-process plugin still runs with the server's authority.  Refusing a contradictory
manifest prevents an author (or an attacker) from describing a plugin as harmless
while it is not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pi_server import plugins

REPO_PLUGINS = Path(__file__).resolve().parent.parent / "plugins"


def _write(tmp_path: Path, manifest: dict) -> Path:
    directory = tmp_path / "plugins" / "candidate"
    directory.mkdir(parents=True)
    (directory / "main.py").write_text("def setup(api):\n    pass\n", encoding="utf-8")
    (directory / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def _scan(tmp_path: Path):
    return plugins.scan(str(tmp_path / "plugins"))


INCONSISTENT = [
    (
        "network without the capability",
        {"id": "p", "entry": "main.py", "capabilities": ["tools"],
         "permissions": {"network": True}},
    ),
    (
        "network host allowlist without the capability",
        {"id": "p", "entry": "main.py", "capabilities": ["tools"],
         "permissions": {"network": ["api.example.com"]}},
    ),
    (
        "filesystem write without the capability",
        {"id": "p", "entry": "main.py", "capabilities": ["tools"],
         "permissions": {"filesystem": "write"}},
    ),
    (
        "filesystem read without the capability",
        {"id": "p", "entry": "main.py", "capabilities": ["tools"],
         "permissions": {"filesystem": "read"}},
    ),
    (
        "process without the capability",
        {"id": "p", "entry": "main.py", "capabilities": ["tools"],
         "permissions": {"process": True}},
    ),
    (
        "secrets without the capability",
        {"id": "p", "entry": "main.py", "capabilities": ["tools"],
         "permissions": {"secrets": ["API_KEY"]}},
    ),
    (
        "an unknown capability name",
        {"id": "p", "entry": "main.py", "capabilities": ["tools", "networking"]},
    ),
]


@pytest.mark.parametrize(
    ("description", "manifest"), INCONSISTENT, ids=[row[0] for row in INCONSISTENT]
)
def test_a_contradictory_manifest_is_refused(
    tmp_path: Path, description: str, manifest: dict
) -> None:
    _write(tmp_path, manifest)

    assert _scan(tmp_path) == [], description


def test_a_consistent_manifest_is_accepted_and_exposes_its_claim(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "id": "honest",
            "entry": "main.py",
            "capabilities": ["tools", "network"],
            "permissions": {"network": ["api.example.com"]},
        },
    )

    found = _scan(tmp_path)

    assert len(found) == 1
    assert found[0].capabilities == ("tools", "network")
    assert found[0].network_hosts == ("api.example.com",)
    assert found[0].to_json()["networkHosts"] == ["api.example.com"]


def test_a_plugin_with_no_permissions_needs_only_tools(tmp_path: Path) -> None:
    _write(tmp_path, {"id": "plain", "entry": "main.py", "capabilities": ["tools"]})

    assert [plugin.id for plugin in _scan(tmp_path)] == ["plain"]


def test_every_shipped_plugin_loads_and_its_capabilities_cover_its_permissions() -> None:
    """The invariant over this repository's own manifests, not just fixtures."""
    manifests = sorted(REPO_PLUGINS.glob("*/plugin.json"))
    assert manifests, "the repository ships plugins; this test would be vacuous otherwise"

    for path in manifests:
        data = json.loads(path.read_text(encoding="utf-8"))
        required = plugins._permission_capabilities(plugins._declared_permissions(data))
        missing = required - set(data.get("capabilities") or ())
        assert not missing, f"{path.parent.name} declares {sorted(missing)} in permissions only"

    loaded = {plugin.id for plugin in plugins.scan(str(REPO_PLUGINS))}
    assert loaded == {json.loads(path.read_text(encoding="utf-8"))["id"] for path in manifests}


def test_network_hosts_are_published_for_review() -> None:
    by_id = {plugin.id: plugin for plugin in plugins.scan(str(REPO_PLUGINS))}

    assert by_id["pubmed"].network_hosts == ("eutils.ncbi.nlm.nih.gov",)
    # ``network: true`` means "anywhere" and has no host list to publish.
    assert by_id["web-fetch"].network_hosts == ()
