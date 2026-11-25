"""A plugin manifest is untrusted input, so it gets attacked here.

Plugins run in-process with the server's full authority (documented, not fixed), so
the *loader* has one job: never let a manifest name code the packager did not put
inside the plugin directory.  These tests are the table of payloads that must be
rejected; each one is a way to make `entry` point outside the package.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pi_server import plugins

#: ``(description, manifest)`` pairs the loader must refuse.
BAD_MANIFESTS = [
    (
        "parent traversal",
        {"id": "evil", "entry": "../evil.py"},
    ),
    (
        "nested traversal",
        {"id": "evil", "entry": "sub/../../evil.py"},
    ),
    (
        "absolute path",
        {"id": "evil", "entry": "/etc/passwd"},
    ),
    (
        "id with a path separator",
        {"id": "../../evil", "entry": "main.py"},
    ),
    (
        "id with a space",
        {"id": "evil plugin", "entry": "main.py"},
    ),
    (
        "version carrying shell punctuation",
        {"id": "evil", "entry": "main.py", "version": "1.0; rm -rf /"},
    ),
    (
        "entry that does not exist",
        {"id": "evil", "entry": "missing.py"},
    ),
]


def _plugin_dir(tmp_path: Path, manifest: dict) -> Path:
    directory = tmp_path / "plugins" / "candidate"
    directory.mkdir(parents=True)
    (directory / "main.py").write_text("def setup(api):\n    pass\n", encoding="utf-8")
    (directory / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


@pytest.mark.parametrize(("description", "manifest"), BAD_MANIFESTS, ids=[item[0] for item in BAD_MANIFESTS])
def test_a_manifest_that_points_outside_the_plugin_is_skipped(
    tmp_path: Path, description: str, manifest: dict
) -> None:
    _plugin_dir(tmp_path, manifest)

    assert plugins.scan(str(tmp_path / "plugins")) == [], description


def test_a_symlinked_entry_is_rejected_even_though_the_path_looks_local(tmp_path: Path) -> None:
    """The one a plain ``..``-check cannot see: a link inside the directory."""
    outside = tmp_path / "outside.py"
    outside.write_text("raise SystemExit('owned')\n", encoding="utf-8")
    directory = _plugin_dir(tmp_path, {"id": "sneaky", "entry": "main.py"})
    os.unlink(directory / "main.py")
    os.symlink(outside, directory / "main.py")

    assert plugins.scan(str(tmp_path / "plugins")) == []


def test_a_directory_symlink_that_escapes_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "main.py").write_text("pass\n", encoding="utf-8")
    (outside / "plugin.json").write_text(json.dumps({"id": "linked", "entry": "main.py"}), encoding="utf-8")
    root = tmp_path / "plugins"
    root.mkdir()
    os.symlink(outside, root / "linked")
    os.unlink(outside / "main.py")
    os.symlink(tmp_path / "outside.py", outside / "main.py")
    (tmp_path / "outside.py").write_text("pass\n", encoding="utf-8")

    assert plugins.scan(str(root)) == []


def test_a_well_formed_plugin_is_accepted(tmp_path: Path) -> None:
    """The negative control: the checks above must not reject everything."""
    _plugin_dir(tmp_path, {"id": "good-one", "name": "Good", "entry": "main.py", "capabilities": ["tools"]})

    found = plugins.scan(str(tmp_path / "plugins"))

    assert [plugin.id for plugin in found] == ["good-one"]
    assert found[0].capabilities == ("tools",)
    assert Path(found[0].entry_path).is_file()


def test_entry_paths_only_returns_the_requested_plugins(tmp_path: Path) -> None:
    _plugin_dir(tmp_path, {"id": "first", "entry": "main.py"})
    second = tmp_path / "plugins" / "second"
    second.mkdir()
    (second / "main.py").write_text("pass\n", encoding="utf-8")
    (second / "plugin.json").write_text(json.dumps({"id": "second", "entry": "main.py"}), encoding="utf-8")

    paths = plugins.entry_paths(str(tmp_path / "plugins"), ["second"])

    assert len(paths) == 1
    assert paths[0].endswith("second/main.py")


def test_a_broken_manifest_does_not_hide_its_neighbours(tmp_path: Path) -> None:
    """One malformed file must not make discovery fail, as on plugin install."""
    _plugin_dir(tmp_path, {"id": "broken", "entry": "../outside.py"})
    good = tmp_path / "plugins" / "good"
    good.mkdir()
    (good / "main.py").write_text("pass\n", encoding="utf-8")
    (good / "plugin.json").write_text(json.dumps({"id": "good", "entry": "main.py"}), encoding="utf-8")

    assert [plugin.id for plugin in plugins.scan(str(tmp_path / "plugins"))] == ["good"]
