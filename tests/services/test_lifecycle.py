from pathlib import Path
from types import SimpleNamespace

import pytest

from tts_studio.runtime import CoreRunRecord, CoreRunStore, core_health_url
from tts_studio.services.lifecycle import (
    LifecycleAdapter,
    LifecycleOperationError,
    LifecycleUnsupportedError,
)
from tts_studio.storage.layout import StorageLayout


class FakeManager:
    def __init__(self, definition: Path | None = None) -> None:
        self.paths = SimpleNamespace(definition=definition)
        self.installs = 0
        self.uninstalls = 0

    def install(self) -> None:
        self.installs += 1

    def uninstall(self) -> bool:
        self.uninstalls += 1
        return True


def _store(tmp_path: Path) -> CoreRunStore:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    return CoreRunStore(layout)


def _record(store: CoreRunStore, pid: int = 1) -> CoreRunRecord:
    from datetime import UTC, datetime

    return CoreRunRecord(
        pid, "127.0.0.1", 7860, str(store._layout.root), datetime.now(UTC).isoformat()
    )


def test_status_reconciles_live_record_and_probes_healthy_core(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    store.write(_record(store))
    monkeypatch.setattr(store, "is_process_alive", lambda pid: True)
    manager = FakeManager(tmp_path / "service")
    manager.paths.definition.touch()

    adapter = LifecycleAdapter(manager, store, health_probe=lambda url: url.endswith(":7860"))

    snapshot = adapter.status()

    assert snapshot.status == "healthy"
    assert snapshot.installed is True
    assert snapshot.running is True
    assert snapshot.healthy is True


def test_status_reports_unavailable_core_and_removes_stale_record(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    store.write(_record(store))
    monkeypatch.setattr(store, "is_process_alive", lambda pid: False)
    manager = FakeManager(tmp_path / "service")

    snapshot = LifecycleAdapter(manager, store, health_probe=lambda url: True).status()

    assert snapshot.status == "not_installed"
    assert snapshot.running is False
    assert not store.path.exists()


def test_install_delegates_to_manager_without_constructing_commands(tmp_path: Path) -> None:
    manager = FakeManager()
    result = LifecycleAdapter(manager, _store(tmp_path)).install()

    assert manager.installs == 1
    assert result.operation == "install"


def test_install_rejects_activation_from_running_in_process_core(tmp_path: Path) -> None:
    manager = FakeManager()

    with pytest.raises(LifecycleUnsupportedError, match="already running"):
        LifecycleAdapter(manager, _store(tmp_path), in_process=True).install()

    assert manager.installs == 0


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", "http://127.0.0.1:7860"),
        ("0.0.0.0", "http://127.0.0.1:7860"),
        ("::1", "http://[::1]:7860"),
        ("::", "http://[::1]:7860"),
        ("0:0:0:0:0:0:0:0", "http://[::1]:7860"),
        ("2001:db8::10", "http://[2001:db8::10]:7860"),
        ("192.0.2.10", "http://192.0.2.10:7860"),
    ],
)
def test_core_health_url_normalizes_wildcards_and_ipv6(host: str, expected: str) -> None:
    assert core_health_url(host, 7860) == expected


def test_restart_is_explicitly_unsupported_in_process(tmp_path: Path) -> None:
    with pytest.raises(LifecycleUnsupportedError, match="unsupported"):
        LifecycleAdapter(FakeManager(), _store(tmp_path)).restart()


def test_uninstall_delegates_and_stops_live_record(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    store.write(_record(store))
    monkeypatch.setattr(store, "is_process_alive", lambda pid: True)
    manager = FakeManager(tmp_path / "service")
    calls: list[tuple[int, float]] = []

    def stop(store_arg, record, timeout):
        assert store_arg is store
        calls.append((record.pid, timeout))

    result = LifecycleAdapter(manager, store, stop_record=stop, timeout=3.0).uninstall()

    assert result.operation == "uninstall"
    assert calls == [(1, 3.0)]
    assert manager.uninstalls == 1


def test_uninstall_rejects_live_record_without_stop_seam(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    store.write(_record(store))
    monkeypatch.setattr(store, "is_process_alive", lambda pid: True)

    with pytest.raises(LifecycleUnsupportedError, match="cannot stop"):
        LifecycleAdapter(FakeManager(), store).uninstall()


def test_live_but_unavailable_core_is_reported(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    store.write(_record(store))
    monkeypatch.setattr(store, "is_process_alive", lambda pid: True)

    snapshot = LifecycleAdapter(
        FakeManager(tmp_path / "service"), store, health_probe=lambda url: False
    ).status()

    assert snapshot.status == "unavailable"
    assert snapshot.running is True
    assert snapshot.healthy is False


def test_uninstall_failure_is_converted_to_safe_error(tmp_path: Path) -> None:
    class BrokenManager(FakeManager):
        def uninstall(self) -> bool:
            raise OSError("secret command details")

    with pytest.raises(LifecycleOperationError, match="could not uninstall") as raised:
        LifecycleAdapter(BrokenManager(), _store(tmp_path)).uninstall()
    assert "secret command" not in str(raised.value)


def test_manager_failure_is_converted_to_safe_error(tmp_path: Path) -> None:
    class BrokenManager(FakeManager):
        def install(self) -> None:
            raise OSError("secret command details")

    with pytest.raises(LifecycleOperationError, match="could not install") as raised:
        LifecycleAdapter(BrokenManager(), _store(tmp_path)).install()
    assert "secret command" not in str(raised.value)
