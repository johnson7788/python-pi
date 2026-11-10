"""Worker capacity: proactive recycling, per-user caps and concurrent acquires."""

from __future__ import annotations

import asyncio

import pytest

from pi_coding_agent.bootstrap import BootstrapOptions
from pi_server.app import CapacityError, SessionRegistry, SessionWorker
from pi_server.config import ServerConfig


def _registry(tmp_path, monkeypatch, **overrides):
    calls = {"started": 0, "stopped": 0}

    async def _start(self):
        calls["started"] += 1

    async def _stop(self, reason="", **kwargs):
        calls["stopped"] += 1

    monkeypatch.setattr(SessionWorker, "ensure_started", _start)
    monkeypatch.setattr(SessionWorker, "shutdown", _stop)
    config = ServerConfig(
        data_root=str(tmp_path),
        auth_mode="team",
        **overrides,
    )
    registry = SessionRegistry(
        BootstrapOptions(cwd=str(tmp_path), agent_dir=str(tmp_path / "agent")), config
    )
    return registry, calls


def test_switching_composition_recycles_the_previous_worker(tmp_path, monkeypatch):
    registry, calls = _registry(tmp_path, monkeypatch)

    async def exercise():
        await registry.acquire("u1", "default", "general", "office")
        await registry.acquire("u1", "default", "research", "review")

    asyncio.run(exercise())
    assert list(registry._workers) == [("u1", "default", "research", "review")]
    assert calls["stopped"] == 1


def test_a_worker_with_an_open_tab_is_kept(tmp_path, monkeypatch):
    registry, _ = _registry(tmp_path, monkeypatch)

    async def exercise():
        first = await registry.acquire("u1", "default", "general", "office")
        # A second browser tab on another app looks like this.
        first.hub._clients.append(object())
        await registry.acquire("u1", "default", "research", "review")
        return first

    first = asyncio.run(exercise())
    assert ("u1", "default", "general", "office") in registry._workers
    assert first.hub.client_count == 1


def test_per_user_cap_evicts_the_least_recently_used_idle_worker(tmp_path, monkeypatch):
    registry, _ = _registry(tmp_path, monkeypatch, max_workers_per_user=2)

    async def exercise():
        first = await registry.acquire("u1", "default", "general", "office")
        first.hub._clients.append(object())  # keep it alive across proactive recycling
        second = await registry.acquire("u1", "default", "research", "review")
        second.hub._clients.append(object())
        await registry.acquire("u1", "default", "slides", "deck")

    asyncio.run(exercise())
    assert len(registry._workers) == 2
    assert ("u1", "default", "slides", "deck") in registry._workers


def test_all_busy_workers_refuse_a_new_composition(tmp_path, monkeypatch):
    registry, _ = _registry(tmp_path, monkeypatch, max_workers_per_user=1)
    monkeypatch.setattr(SessionWorker, "busy", property(lambda self: True))

    async def exercise():
        await registry.acquire("u1", "default", "general", "office")
        with pytest.raises(CapacityError):
            await registry.acquire("u1", "default", "research", "review")

    asyncio.run(exercise())


def test_active_user_cap_counts_users_not_workers(tmp_path, monkeypatch):
    registry, _ = _registry(tmp_path, monkeypatch, max_active_users=1)

    async def exercise():
        await registry.acquire("u1", "default", "general", "office")
        # Same user, another composition: still allowed.
        await registry.acquire("u1", "default", "research", "review")
        with pytest.raises(CapacityError):
            await registry.acquire("u2", "default", "general", "office")

    asyncio.run(exercise())


def test_concurrent_acquires_for_one_key_start_a_single_worker(tmp_path, monkeypatch):
    registry, calls = _registry(tmp_path, monkeypatch)

    async def exercise():
        return await asyncio.gather(
            *(registry.acquire("u1", "default", "general", "office") for _ in range(8))
        )

    workers = asyncio.run(exercise())
    assert len(registry._workers) == 1
    assert calls["started"] == 1
    assert all(worker is workers[0] for worker in workers)
