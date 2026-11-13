# SPDX-License-Identifier: Apache-2.0
"""Cases for interrupted weight updates."""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from sglang_omni_router.python.config import RouterConfig, WorkerConfig
from sglang_omni_router.python.control_plane import create_control_plane_app
from sglang_omni_router.python.update_journal import UpdateJournal


def make_config(failure_threshold: int = 1) -> RouterConfig:
    return RouterConfig(
        workers=[WorkerConfig(url="http://worker-a:8101")],
        health_failure_threshold=failure_threshold,
        health_success_threshold=1,
    )


class MockUpstream:
    """Mock worker upstream tracking admin calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy"})
        return httpx.Response(
            200, json={"success": True, "message": "ok", "results": []}
        )


def cp_app(tmp_path: Path, upstream: MockUpstream | None = None, **kwargs):
    upstream = upstream or MockUpstream()
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler))
    snapshot_path = str(tmp_path / "workers.json")
    app = create_control_plane_app(
        make_config(**{k: v for k, v in kwargs.items() if k == "failure_threshold"}),
        snapshot_path=snapshot_path,
        cp_epoch="epoch-test",
        client=client,
        **{k: v for k, v in kwargs.items() if k != "failure_threshold"},
    )
    return app, snapshot_path, upstream


def test_worker_state_after_interrupted_update(tmp_path: Path) -> None:
    from sglang_omni_router.python.app import restore_admin_disabled_state
    from sglang_omni_router.python.worker import build_workers

    workers = build_workers(
        [
            WorkerConfig(url="http://worker-a:8101"),
            WorkerConfig(url="http://worker-b:8102"),
        ]
    )
    previous = {w.worker_id: False for w in workers}
    restore_admin_disabled_state(workers, previous, outcome_safe=False)
    assert all(w.disabled for w in workers)


def test_broadcast_state_after_interrupted_update(tmp_path: Path) -> None:
    class CrashingUpstream(MockUpstream):
        def handler(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/update_weights_from_disk":
                raise RuntimeError("update stream reset")
            else:
                return super().handler(request)

    journal_path = str(tmp_path / "j.json")
    app, _, _ = cp_app(tmp_path, upstream=CrashingUpstream(), journal_path=journal_path)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/workers", json={"url": "http://worker-b:8102"})

        client.post("/update_weights_from_disk", json={"path": "/m"})

        workers = app.state.workers
        assert len(workers) == 2
        assert all(worker.disabled for worker in workers)
        assert sorted(UpdateJournal(journal_path).pending()) == sorted(
            worker.worker_id for worker in workers
        )
