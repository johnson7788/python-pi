"""The per-user workspace, and the only place a caller's string becomes a filesystem path.

This module exists because the browser now asks for files by name, and a name is
attacker-controlled the moment one colleague can talk to a server another colleague
also uses.  Every entry point therefore funnels through :meth:`Workspace.path_for`,
which applies three rules in order:

1. **Normalise.**  The path is always workspace-relative.  A leading ``/`` means the
   workspace root (so the UI can display ``/notes/todo.md``), but an absolute path, a
   ``..`` element or a NUL byte is rejected outright rather than clamped.
2. **Resolve symlinks.**  Containment is checked against ``realpath``, not the literal
   join, so a symlink planted inside the workspace cannot be used as a door out of it.
   Writing a *new* file resolves the parent, which closes the same door on create.
3. **Contain.**  The resolved path must still equal the root or sit beneath it.

The read/write caps are here rather than in the HTTP layer so that a limit cannot be
bypassed by a caller that reaches this module directly.

There is deliberately no multipart handling: an upload is a ``PUT`` of raw bytes to a
path, which also makes saving a text edit and uploading a binary the same code path and
keeps ``python-multipart`` out of the dependency list.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterator

#: Largest file the API will write or hand to a browser in one request.
MAX_WRITE_BYTES = 32 * 1024 * 1024

#: Largest file whose *text* is returned inline; bigger files report metadata only.
MAX_PREVIEW_BYTES = 512 * 1024

#: Cap on entries returned by one tree call, so a huge directory cannot stall the UI.
MAX_TREE_ENTRIES = 5000


class WorkspaceError(Exception):
    """Base class for failures that map onto an HTTP status."""

    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidPath(WorkspaceError):
    """The requested path is malformed or leaves the workspace."""

    status_code = 400


class NotFound(WorkspaceError):
    """The requested path does not exist."""

    status_code = 404


class TooLarge(WorkspaceError):
    """The payload or the file exceeds a configured cap."""

    status_code = 413


def _within(root: str, candidate: str) -> bool:
    if candidate == root:
        return True
    return candidate.startswith(root + os.sep)


def normalize_relative(relative: str | None) -> tuple[str, ...]:
    """Turn a caller-supplied path into safe, workspace-relative parts.

    ``"/"``, ``""`` and ``None`` all mean the root and yield ``()``.
    """
    raw = (relative or "/").strip()
    if "\x00" in raw:
        raise InvalidPath("path contains a NUL byte")
    if raw.startswith("/"):
        raw = raw.lstrip("/")
    if raw in ("", "."):
        return ()
    # A Windows-style drive or UNC prefix would be meaningless here and is a sign the
    # caller is confused about what these paths are.
    if ":" in raw:
        raise InvalidPath("path must be workspace-relative")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        raise InvalidPath(f"path must not leave the workspace: {relative!r}")
    return tuple(part for part in pure.parts if part not in ("", "."))


def valid_child_name(name: str) -> str:
    """Validate a single path segment supplied on its own (new file or folder)."""
    candidate = (name or "").strip()
    if not candidate or candidate in (".", ".."):
        raise InvalidPath("a name is required")
    if "/" in candidate or "\\" in candidate or "\x00" in candidate:
        raise InvalidPath("a name cannot contain a path separator")
    return candidate


@dataclass(frozen=True)
class Entry:
    name: str
    path: str
    is_dir: bool
    size: int
    modified: float
    hidden: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "isDir": self.is_dir,
            "size": self.size,
            "modified": self.modified,
            "hidden": self.hidden,
        }


class Workspace:
    """One user's working directory, addressed only through workspace-relative paths."""

    def __init__(self, root: str, *, create: bool = True) -> None:
        self.root = os.path.abspath(os.path.expanduser(root))
        if create:
            os.makedirs(self.root, exist_ok=True)
        if not os.path.isdir(self.root):
            raise NotFound(f"workspace does not exist: {self.root}")

    # -- paths -----------------------------------------------------------------------

    def path_for(self, relative: str | None, *, must_exist: bool = False) -> str:
        """The filesystem path for ``relative``, guaranteed to be inside the workspace."""
        parts = normalize_relative(relative)
        candidate = os.path.join(self.root, *parts) if parts else self.root
        root_real = os.path.realpath(self.root)
        # Resolving the *parent* for a not-yet-existing file is what stops a symlinked
        # directory from being used to write outside the workspace.
        subject = candidate if os.path.exists(candidate) else os.path.dirname(candidate) or candidate
        if not _within(root_real, os.path.realpath(subject)):
            raise InvalidPath(f"path must not leave the workspace: {relative!r}")
        if must_exist and not os.path.exists(candidate):
            raise NotFound(f"no such path: {relative}")
        return candidate

    def display_path(self, relative: str | None) -> str:
        parts = normalize_relative(relative)
        return "/" + "/".join(parts) if parts else "/"

    # -- reads -----------------------------------------------------------------------

    def tree(
        self, relative: str | None = "/", *, recursive: bool = False, files_only: bool = False
    ) -> dict[str, Any]:
        base = self.path_for(relative, must_exist=True)
        if not os.path.isdir(base):
            raise InvalidPath(f"not a directory: {self.display_path(relative)}")

        entries: list[Entry] = []
        truncated = False
        for path, is_dir, size, modified in self._walk(base, recursive=recursive):
            if files_only and is_dir:
                continue
            if len(entries) >= MAX_TREE_ENTRIES:
                truncated = True
                break
            entries.append(
                Entry(
                    name=os.path.basename(path) or self.display_path(path),
                    path=self.display_path(os.path.relpath(path, self.root)),
                    is_dir=is_dir,
                    size=size,
                    modified=modified,
                    hidden=os.path.basename(path).startswith("."),
                )
            )
        entries.sort(key=lambda entry: (not entry.is_dir, entry.name.lower()))
        return {
            "path": self.display_path(relative),
            "entries": [entry.to_json() for entry in entries],
            "truncated": truncated,
        }

    def _walk(self, base: str, *, recursive: bool) -> Iterator[tuple[str, bool, int, float]]:
        """Yield ``(path, is_dir, size, mtime)`` without following symlinks out of the tree."""
        try:
            names = sorted(os.listdir(base))
        except PermissionError as error:
            raise NotFound(f"cannot read directory: {error}") from error
        for name in names:
            path = os.path.join(base, name)
            try:
                # `lstat` deliberately: a symlink is listed as a link, not followed.
                stat = os.lstat(path)
            except OSError:
                continue
            is_dir = os.path.isdir(path) and not os.path.islink(path)
            yield path, is_dir, 0 if is_dir else stat.st_size, stat.st_mtime
            if recursive and is_dir:
                yield from self._walk(path, recursive=True)

    def read(self, relative: str | None) -> dict[str, Any]:
        path = self.path_for(relative, must_exist=True)
        if os.path.isdir(path):
            raise InvalidPath(f"not a file: {self.display_path(relative)}")
        stat = os.stat(path)
        payload: dict[str, Any] = {
            "path": self.display_path(relative),
            "name": os.path.basename(path),
            "size": stat.st_size,
            "modified": stat.st_mtime,
            "binary": False,
            "truncated": False,
            "text": None,
        }
        if stat.st_size > MAX_PREVIEW_BYTES:
            payload.update(binary=False, truncated=True)
            return payload
        with open(path, "rb") as handle:
            raw = handle.read(MAX_PREVIEW_BYTES)
        try:
            payload["text"] = raw.decode("utf-8")
        except UnicodeDecodeError:
            payload["binary"] = True
        return payload

    def download_target(self, relative: str | None) -> tuple[str, str]:
        """``(absolute path, suggested filename)`` for a download response."""
        path = self.path_for(relative, must_exist=True)
        if os.path.isdir(path):
            raise InvalidPath("a directory cannot be downloaded")
        return path, os.path.basename(path) or "download"

    # -- writes ----------------------------------------------------------------------

    def write(self, relative: str | None, data: bytes) -> dict[str, Any]:
        if len(data) > MAX_WRITE_BYTES:
            raise TooLarge(f"file exceeds the {MAX_WRITE_BYTES} byte limit")
        path = self.path_for(relative)
        if os.path.isdir(path):
            raise InvalidPath(f"not a file: {self.display_path(relative)}")
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # Re-check after creating parents: `makedirs` cannot escape, but this keeps the
        # containment guarantee in one place regardless of what ran before it.
        self.path_for(relative)
        with open(path, "wb") as handle:
            handle.write(data)
        stat = os.stat(path)
        return {
            "path": self.display_path(relative),
            "name": os.path.basename(path),
            "size": stat.st_size,
            "modified": stat.st_mtime,
        }

    def create_directory(self, parent: str | None, name: str) -> dict[str, Any]:
        child = valid_child_name(name)
        base = self.path_for(parent, must_exist=True)
        if not os.path.isdir(base):
            raise InvalidPath(f"not a directory: {self.display_path(parent)}")
        target = os.path.join(base, child)
        if os.path.exists(target):
            raise InvalidPath(f"already exists: {child}")
        os.makedirs(target)
        return {"path": self.display_path(os.path.relpath(target, self.root)), "name": child}

    def delete(self, relative: str | None) -> dict[str, Any]:
        parts = normalize_relative(relative)
        if not parts:
            raise InvalidPath("the workspace root cannot be deleted")
        path = self.path_for(relative, must_exist=True)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        return {"ok": True, "path": self.display_path(relative)}


__all__ = [
    "MAX_PREVIEW_BYTES",
    "MAX_TREE_ENTRIES",
    "MAX_WRITE_BYTES",
    "Entry",
    "InvalidPath",
    "NotFound",
    "TooLarge",
    "Workspace",
    "WorkspaceError",
    "normalize_relative",
    "valid_child_name",
]
