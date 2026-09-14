"""The phase-two execution seam: what exists today, and what must fail loudly.

The interesting behaviour here is not the protocol — it is that asking for isolation the
deployment cannot deliver is refused at startup rather than silently ignored.
"""

from __future__ import annotations

import pytest

from pi_coding_agent.sandbox import (
    ExecResult,
    LocalBackend,
    ProvisionerBackend,
    SandboxBackend,
    assert_backend_usable,
    select_backend,
)


def test_local_backend_satisfies_the_protocol():
    """A runtime-checkable protocol is worth checking at runtime, not only in the type checker."""
    assert isinstance(LocalBackend(), SandboxBackend)
    assert isinstance(ProvisionerBackend(), SandboxBackend)


@pytest.mark.asyncio
async def test_local_backend_executes_and_reports(tmp_path):
    backend = LocalBackend()

    ok = await backend.exec("printf 'hello'", cwd=str(tmp_path))
    assert isinstance(ok, ExecResult)
    assert ok.output == "hello"
    assert ok.exit_code == 0

    failed = await backend.exec("exit 3", cwd=str(tmp_path))
    assert failed.exit_code == 3


@pytest.mark.asyncio
async def test_local_backend_reports_a_timeout_instead_of_raising(tmp_path):
    """A remote backend cannot raise the local exception type, so the result carries it."""
    result = await LocalBackend().exec("sleep 5", cwd=str(tmp_path), timeout_seconds=0.2)
    assert result.timed_out is True
    assert result.exit_code is None


def test_local_backend_reads_writes_and_lists(tmp_path):
    backend = LocalBackend()
    target = tmp_path / "nested" / "note.txt"

    backend.write_bytes(str(target), "hi".encode())
    assert backend.exists(str(target))
    assert backend.read_text(str(target)) == "hi"
    assert backend.read_text(str(target), limit=1) == "h"
    assert backend.list_dir(str(tmp_path)) == ["nested"]


def test_selecting_an_unknown_backend_is_an_error_not_a_fallback():
    """Falling back to local would run in-process exactly when isolation was requested."""
    with pytest.raises(ValueError):
        select_backend("quantum")


def test_the_provisioner_is_refused_at_startup():
    backend = select_backend("provisioner")
    assert isinstance(backend, ProvisionerBackend)
    with pytest.raises(NotImplementedError) as info:
        assert_backend_usable(backend)
    # The message has to say where the plan lives, or it is just an error.
    assert "design.md" in str(info.value)


def test_local_is_the_default_and_is_usable(monkeypatch):
    monkeypatch.delenv("PI_SANDBOX_BACKEND", raising=False)
    backend = select_backend()
    assert backend.name == "local"
    assert_backend_usable(backend)
