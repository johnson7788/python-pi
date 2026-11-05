"""Office editor document ids and storage helpers.

A document id is an opaque base64url JSON envelope carrying the workspace id and
the workspace-relative path.  Nothing outside this module needs to know the
mapping, and absolute filesystem paths never reach the browser.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass

OFFICE_EXTENSIONS = (".docx", ".xlsx", ".pptx")
MAX_OFFICE_BYTES = 50 * 1024 * 1024


class OfficeIdError(ValueError):
    pass


@dataclass(frozen=True)
class OfficeRef:
    workspace: str
    path: str


def encode_doc_id(workspace: str, path: str) -> str:
    payload = json.dumps({"w": workspace, "p": path}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_doc_id(doc_id: str) -> OfficeRef:
    try:
        padded = doc_id + "=" * ((4 - len(doc_id) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception as error:  # noqa: BLE001
        raise OfficeIdError("invalid document id") from error
    if not isinstance(payload, dict):
        raise OfficeIdError("invalid document id")
    workspace = str(payload.get("w") or "").strip()
    path = str(payload.get("p") or "").strip().lstrip("/")
    if not workspace or not path or ".." in path.split("/"):
        raise OfficeIdError("invalid document id")
    if not any(path.lower().endswith(ext) for ext in OFFICE_EXTENSIONS):
        raise OfficeIdError("unsupported office extension")
    return OfficeRef(workspace=workspace, path=path)


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def backup_version(root: str, doc_id: str, path: str) -> None:
    """Copy the current file into the workspace's hidden versions directory."""
    if not os.path.isfile(path):
        return
    target_dir = os.path.join(root, ".pi", "versions", doc_id)
    os.makedirs(target_dir, exist_ok=True)
    stamp = str(time.time_ns())
    target = os.path.join(target_dir, f"{stamp}{os.path.splitext(path)[1]}")
    if not os.path.exists(target):
        shutil.copyfile(path, target)


def atomic_write(path: str, data: bytes) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temp = f"{path}.tmp-{os.getpid()}"
    with open(temp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
