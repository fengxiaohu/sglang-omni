# SPDX-License-Identifier: Apache-2.0
"""Cases for interrupted weight updates."""

from __future__ import annotations

from pathlib import Path


from sglang_omni_router.python.app import restore_admin_disabled_state
from sglang_omni_router.python.config import WorkerConfig
from sglang_omni_router.python.worker import build_workers


def test_broadcast_crash_keeps_targets_disabled_and_journaled(tmp_path: Path) -> None:
    workers = build_workers(
        [
            WorkerConfig(url="http://worker-a:8101"),
            WorkerConfig(url="http://worker-b:8102"),
        ]
    )
    previous = {w.worker_id: False for w in workers}
    restore_admin_disabled_state(workers, previous, outcome_safe=False)
    assert all(w.disabled for w in workers)
