import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest

from tts_studio.runtime import CoreRunRecord, CoreRunStore
from tts_studio.storage.layout import StorageLayout


def _record(data_dir: Path, pid: int = 1234) -> CoreRunRecord:
    return CoreRunRecord(
        pid=pid,
        host="127.0.0.1",
        port=7860,
        data_dir=str(data_dir),
        started_at=datetime.now(UTC).isoformat(),
    )


def test_run_store_round_trips_private_record(tmp_path: Path) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    store = CoreRunStore(layout)

    record = _record(layout.root)
    store.write(record)

    assert store.read() == record
    assert store.path.stat().st_mode & 0o077 == 0


def test_empty_run_store_is_safe_before_first_core_start(tmp_path: Path) -> None:
    store = CoreRunStore(StorageLayout.from_root(tmp_path / "data"))

    assert store.read() is None
    assert store.tail_logs() == ""


def test_run_store_removes_stale_record(tmp_path: Path, monkeypatch) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    store = CoreRunStore(layout)
    store.write(_record(layout.root))
    monkeypatch.setattr(store, "is_process_alive", lambda pid: False)

    assert store.reconcile() is None
    assert not store.path.exists()


def test_run_store_preserves_live_record(tmp_path: Path, monkeypatch) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    store = CoreRunStore(layout)
    record = _record(layout.root)
    store.write(record)
    monkeypatch.setattr(store, "is_process_alive", lambda pid: True)

    assert store.reconcile() == record
    assert store.read() == record


def test_concurrent_claims_allow_only_one_owner(tmp_path: Path) -> None:
    stores = [CoreRunStore(StorageLayout.from_root(tmp_path / "data")) for _ in range(2)]
    start = Barrier(2)
    attempted = Barrier(2)

    def attempt(store: CoreRunStore) -> str:
        start.wait()
        claim = None
        try:
            claim = store.claim()
            result = "claimed"
        except RuntimeError:
            result = "duplicate"
        finally:
            attempted.wait()
            if claim is not None:
                claim.release()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, stores))

    assert sorted(results) == ["claimed", "duplicate"]


def test_reconcile_preserves_replacement_written_before_locked_reread(
    tmp_path: Path, monkeypatch
) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    store = CoreRunStore(layout)
    stale = _record(layout.root, pid=1)
    store.write(stale)
    replacement = _record(layout.root, pid=os.getpid())
    original_open_lock = store._open_lock

    def lock_and_replace(*, create: bool = False) -> int | None:
        descriptor = original_open_lock(create=create)
        assert descriptor is not None
        store.write(replacement)
        return descriptor

    monkeypatch.setattr(store, "_open_lock", lock_and_replace)
    monkeypatch.setattr(store, "is_process_alive", lambda pid: pid == replacement.pid)

    assert store.reconcile() == replacement
    assert store.read() == replacement


def test_clear_preserves_replacement_written_before_locked_compare(
    tmp_path: Path, monkeypatch
) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    store = CoreRunStore(layout)
    original = _record(layout.root, pid=1)
    original = CoreRunRecord(**{**original.__dict__, "owner_token": "old-owner"})
    store.write(original)
    replacement = _record(layout.root, pid=2)
    replacement = CoreRunRecord(**{**replacement.__dict__, "owner_token": "new-owner"})
    setup_claim = store.claim()
    setup_claim.release()
    original_open_lock = store._open_lock

    def lock_and_replace(*, create: bool = False) -> int | None:
        descriptor = original_open_lock(create=create)
        assert descriptor is not None
        store.write(replacement)
        return descriptor

    monkeypatch.setattr(store, "_open_lock", lock_and_replace)

    store.clear_if_owner(original.pid, original.owner_token)

    assert store.read() == replacement


@pytest.mark.asyncio
async def test_snapshot_exposes_worker_startup_diagnostics(tmp_path: Path) -> None:
    from tts_studio.config import Settings
    from tts_studio.runtime import snapshot_runtime

    class Supervisor:
        async def statuses(self):
            return ()

        def startup_diagnostics(self):
            return {"fake": {"status": "failed", "message": "startup failed", "restart_count": 2}}

    snapshot = await snapshot_runtime(Settings.resolve(tmp_path / "data"), Supervisor())

    assert snapshot.startup_diagnostics["fake"]["restart_count"] == 2


def test_safe_worker_diagnostic_does_not_expose_exception_payload(tmp_path: Path) -> None:
    del tmp_path
    from tts_studio.workers.supervisor import _safe_diagnostic

    diagnostic = _safe_diagnostic(RuntimeError("token=secret /private/model payload"))

    assert "secret" not in diagnostic
    assert "/private/model" not in diagnostic


def test_reconcile_rejects_live_unrelated_pid_with_stale_identity(
    tmp_path: Path, monkeypatch
) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    store = CoreRunStore(layout)
    record = _record(layout.root, pid=1)
    record = CoreRunRecord(**{**record.__dict__, "owner_token": "stale-owner"})
    store.write(record)
    monkeypatch.setattr(store, "is_process_alive", lambda pid: True)

    assert store.reconcile() is None
    assert store.read() is None
