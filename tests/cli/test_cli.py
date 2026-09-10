"""Public command-line behavior for the foreground Core."""

from __future__ import annotations

import socket
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from threading import Thread

import httpx
import pytest
import typer
import uvicorn
from typer.testing import CliRunner

from tts_studio import cli
from tts_studio.cli import app
from tts_studio.client import CoreClient, CoreUnavailable
from tts_studio.config import Settings
from tts_studio.runtime import CoreClaim
from tts_studio.server.app import create_app

runner = CliRunner()


@pytest.fixture
def http_server_url(tmp_path: Path) -> Iterator[str]:
    """Run an isolated Core over its actual public HTTP interface."""
    if sys.platform == "win32":
        pytest.skip("threaded Uvicorn Core fixtures cannot start Worker adapters on Windows CI")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    host, port = listener.getsockname()

    server = uvicorn.Server(
        uvicorn.Config(
            create_app(Settings.resolve(tmp_path / ".tts-studio")),
            log_level="critical",
        )
    )
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()

    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        pytest.fail("Core did not start within five seconds")

    try:
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",
        "192.0.2.10",
        "::",
        "localhost",
        "tts-studio.local",
    ],
)
def test_serve_rejects_non_loopback_without_token_configuration_before_app_creation(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
) -> None:
    def unexpected_app_creation(_: Settings) -> object:
        pytest.fail("serve must reject insecure settings before creating the app")

    monkeypatch.delenv("TTS_STUDIO_API_TOKEN_ENV", raising=False)
    monkeypatch.setattr(cli, "create_app", unexpected_app_creation)

    result = runner.invoke(app, ["serve", "--host", host])

    assert result.exit_code == 2
    assert "loopback IP literal" in result.output
    assert "bearer authentication" in result.output


def test_serve_accepts_non_loopback_with_configured_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    token_env = "TTS_STUDIO_TEST_API_TOKEN"
    monkeypatch.setenv("TTS_STUDIO_API_TOKEN_ENV", token_env)
    monkeypatch.setenv(token_env, "private-token-value")
    monkeypatch.setattr(
        cli,
        "create_app",
        lambda settings: captured.setdefault("settings", settings),
    )
    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda application, host, port: captured.update(
            application=application, host=host, port=port
        ),
    )

    result = runner.invoke(
        app, ["serve", "--host", "0.0.0.0", "--data-dir", str(tmp_path / "data")]
    )

    assert result.exit_code == 0
    settings = captured["settings"]
    assert isinstance(settings, Settings)
    assert settings.api_token_env == token_env
    assert captured["host"] == "0.0.0.0"


def test_serve_rejects_external_host_when_resolved_token_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_env = "TTS_STUDIO_TEST_API_TOKEN"
    monkeypatch.setenv("TTS_STUDIO_HOST", "0.0.0.0")
    monkeypatch.setenv("TTS_STUDIO_API_TOKEN_ENV", token_env)
    monkeypatch.delenv(token_env, raising=False)

    def unexpected_app_creation(_: Settings) -> object:
        pytest.fail("serve must reject insecure settings before creating the app")

    monkeypatch.setattr(cli, "create_app", unexpected_app_creation)

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 2
    assert "loopback IP literal" in result.output


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_serve_accepts_loopback_ip_literals(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    host: str,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.delenv("TTS_STUDIO_HOST", raising=False)
    monkeypatch.setattr(
        cli, "create_app", lambda settings: captured.setdefault("settings", settings)
    )
    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda application, host, port: captured.update(
            application=application, host=host, port=port
        ),
    )

    result = runner.invoke(
        app,
        ["serve", "--host", host, "--data-dir", str(tmp_path / "data")],
    )

    assert result.exit_code == 0
    assert captured["host"] == host
    assert captured["application"] is captured["settings"]


def test_foreground_write_failure_releases_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr(cli, "create_app", lambda settings: settings)
    monkeypatch.setattr(cli.CoreRunStore, "write", lambda self, record: (_ for _ in ()).throw(OSError("write failed")))

    result = runner.invoke(app, ["serve", "--data-dir", str(data_dir)])

    assert result.exit_code != 0
    claim = cli.CoreRunStore(cli.StorageLayout.from_root(data_dir)).claim()
    claim.release()


def test_legacy_live_record_rejects_duplicate_foreground_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    store = cli.CoreRunStore(cli.StorageLayout.from_root(data_dir))
    store.write(
        cli.CoreRunRecord(
            pid=cli.os.getpid(),
            host="127.0.0.1",
            port=7860,
            data_dir=str(data_dir.resolve()),
            started_at="2026-09-08T00:00:00+00:00",
        )
    )
    monkeypatch.setattr(cli, "create_app", lambda settings: pytest.fail("must not start"))

    result = runner.invoke(app, ["serve", "--data-dir", str(data_dir)])

    assert result.exit_code == 2
    assert "already running" in result.output


def test_foreground_serve_owns_run_record_for_its_lifetime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    observed: dict[str, object] = {}
    monkeypatch.setattr(cli, "create_app", lambda settings: settings)

    def run(application: object, host: str, port: int) -> None:
        store = cli.CoreRunStore(cli.StorageLayout.from_root(data_dir))
        observed["record"] = store.read()

    monkeypatch.setattr(uvicorn, "run", run)

    result = runner.invoke(app, ["serve", "--data-dir", str(data_dir)])

    assert result.exit_code == 0
    record = observed["record"]
    assert isinstance(record, cli.CoreRunRecord)
    assert record.pid == cli.os.getpid()
    assert cli.CoreRunStore(cli.StorageLayout.from_root(data_dir)).read() is None


def test_service_install_rejects_a_running_core_before_activation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    store = cli.CoreRunStore(cli.StorageLayout.from_root(data_dir))
    store.write(
        cli.CoreRunRecord(
            pid=cli.os.getpid(),
            host="127.0.0.1",
            port=7860,
            data_dir=str(data_dir.resolve()),
            started_at="2026-09-08T00:00:00+00:00",
        )
    )
    monkeypatch.setattr(
        cli,
        "_service_manager",
        lambda *_args, **_kwargs: pytest.fail("service activation must not be attempted"),
    )

    result = runner.invoke(app, ["service", "install", "--data-dir", str(data_dir)])

    assert result.exit_code == 2
    assert "already running" in result.output


def test_serve_background_starts_without_running_uvicorn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli,
        "_start_background",
        lambda settings, include_test_adapters: captured.update(
            settings=settings, include_test_adapters=include_test_adapters
        ),
    )
    monkeypatch.setattr(
        uvicorn, "run", lambda *_args, **_kwargs: pytest.fail("foreground server started")
    )

    result = runner.invoke(
        app,
        ["serve", "--background", "--data-dir", str(tmp_path / "data")],
    )

    assert result.exit_code == 0
    assert isinstance(captured["settings"], Settings)
    assert captured["include_test_adapters"] is False


def test_restart_stops_existing_record_before_starting_background(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    record = cli.CoreRunRecord(
        pid=4321,
        host="127.0.0.1",
        port=7860,
        data_dir=str(data_dir),
        started_at="2026-01-01T00:00:00+00:00",
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli.CoreRunStore, "reconcile", lambda self: record)
    monkeypatch.setattr(
        cli,
        "_stop_record",
        lambda store, found, timeout: captured.update(stopped=found, timeout=timeout),
    )
    monkeypatch.setattr(
        cli,
        "_start_background",
        lambda settings, include_test_adapters: captured.update(
            settings=settings, include_test_adapters=include_test_adapters
        ),
    )

    result = runner.invoke(
        app,
        [
            "restart",
            "--data-dir",
            str(data_dir),
            "--timeout",
            "2.5",
            "--include-test-adapters",
        ],
    )

    assert result.exit_code == 0
    assert captured["stopped"] == record
    assert captured["timeout"] == 2.5
    assert captured["include_test_adapters"] is True


def test_background_log_fd_failure_releases_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    original_open = cli.os.open

    def fail_log_open(path, *args, **kwargs):
        if str(path).endswith("core.log"):
            raise OSError("log open failed")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(cli.os, "open", fail_log_open)

    with pytest.raises(OSError, match="log open failed"):
        cli._start_background(Settings.resolve(data_dir), include_test_adapters=False)

    claim = cli.CoreRunStore(cli.StorageLayout.from_root(data_dir)).claim()
    claim.release()


def test_background_start_clears_child_record_on_readiness_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    captured: dict[str, object] = {}
    original_claim = cli.CoreRunStore.claim

    class FakeProcess:
        pid = 9877

        def terminate(self) -> None:
            return None

    def claim(store):
        value = original_claim(store)
        captured["store"] = store
        captured["claim"] = value
        return value

    monkeypatch.setattr(cli.CoreRunStore, "claim", claim)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(cli.CoreRunStore, "is_process_alive", staticmethod(lambda pid: True))

    def fail_wait(settings, process=None):
        store = captured["store"]
        claim_value = captured["claim"]
        assert isinstance(store, cli.CoreRunStore)
        assert isinstance(claim_value, CoreClaim)
        store.write(
            cli.CoreRunRecord(
                pid=process.pid,
                host=settings.host,
                port=settings.port,
                data_dir=str(settings.data_dir),
                started_at="2026-09-09T00:00:00+00:00",
                owner_token=claim_value.token,
            )
        )
        raise RuntimeError("readiness failed")

    monkeypatch.setattr(cli, "_wait_for_core", fail_wait)

    with pytest.raises(typer.BadParameter, match="readiness failed"):
        cli._start_background(Settings.resolve(data_dir), include_test_adapters=False)

    store = cli.CoreRunStore(cli.StorageLayout.from_root(data_dir))
    assert store.read() is None
    claim = store.claim()
    claim.release()


def test_background_start_handoffs_claim_fd_and_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 4321

        def poll(self) -> None:
            return None

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["pass_fds"] = kwargs["pass_fds"]
        return FakeProcess()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli, "_wait_for_core", lambda settings, process=None: None)

    cli._start_background(Settings.resolve(tmp_path / "data"), include_test_adapters=False)

    command = captured["command"]
    assert isinstance(command, list)
    assert "--claim-fd" in command
    assert "--claim-token" in command
    assert captured["pass_fds"]


def test_background_start_cleans_up_when_core_never_becomes_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    terminated: list[int] = []

    class FakeProcess:
        pid = 9876

        def terminate(self) -> None:
            terminated.append(self.pid)

    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(cli.CoreRunStore, "is_process_alive", staticmethod(lambda pid: True))
    monkeypatch.setattr(
        cli,
        "_wait_for_core",
        lambda settings, process=None: (_ for _ in ()).throw(
            RuntimeError("port is already in use")
        ),
    )

    with pytest.raises(typer.BadParameter, match="port is already in use"):
        cli._start_background(Settings.resolve(data_dir), include_test_adapters=False)

    store = cli.CoreRunStore(cli.StorageLayout.from_root(data_dir))
    assert terminated == [9876]
    assert store.read() is None


def test_stop_signals_core_when_owner_claim_is_held(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = cli.CoreRunStore(cli.StorageLayout.from_root(tmp_path / "data"))
    claim = store.claim()
    record = cli.CoreRunRecord(
        pid=9876,
        host="127.0.0.1",
        port=7860,
        data_dir=str(tmp_path / "data"),
        started_at="2026-09-08T00:00:00+00:00",
        owner_token=claim.token,
    )
    store.write(record)
    signaled: list[tuple[int, int]] = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: signaled.append((pid, sig)))
    try:
        cli._stop_record(store, record, timeout=0)
    finally:
        claim.release()
    assert signaled[0] == (9876, cli.signal.SIGTERM)


def test_stop_forces_core_without_sigkill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = cli.CoreRunStore(cli.StorageLayout.from_root(tmp_path / "data"))
    claim = store.claim()
    record = cli.CoreRunRecord(
        pid=9876,
        host="127.0.0.1",
        port=7860,
        data_dir=str(tmp_path / "data"),
        started_at="2026-09-08T00:00:00+00:00",
        owner_token=claim.token,
    )
    store.write(record)
    signaled: list[tuple[int, int]] = []
    monkeypatch.delattr(cli.signal, "SIGKILL")
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: signaled.append((pid, sig)))
    try:
        cli._stop_record(store, record, timeout=0)
    finally:
        claim.release()

    assert [signal for _, signal in signaled if signal != 0] == [
        cli.signal.SIGTERM,
        cli.signal.SIGTERM,
    ]


def test_stop_releases_claim_when_clear_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = cli.CoreRunStore(cli.StorageLayout.from_root(tmp_path / "data"))
    claim = store.claim()
    record = cli.CoreRunRecord(
        pid=9876,
        host="127.0.0.1",
        port=7860,
        data_dir=str(tmp_path / "data"),
        started_at="2026-09-08T00:00:00+00:00",
        owner_token=claim.token,
    )
    store.write(record)
    monkeypatch.setattr(store, "acquire_owner", lambda found: claim)
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError))
    monkeypatch.setattr(
        store,
        "clear_if_owner",
        lambda *args: (_ for _ in ()).throw(OSError("clear failed")),
    )

    with pytest.raises(OSError, match="clear failed"):
        cli._stop_record(store, record, timeout=0)

    claim.release()
    replacement = store.claim()
    replacement.release()


def test_stop_does_not_signal_when_run_record_changed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = cli.CoreRunStore(cli.StorageLayout.from_root(tmp_path / "data"))
    store._layout.ensure()
    record = cli.CoreRunRecord(
        pid=9876,
        host="127.0.0.1",
        port=7860,
        data_dir=str(tmp_path / "data"),
        started_at="2026-09-08T00:00:00+00:00",
        owner_token="owner-token",
    )
    replacement = cli.CoreRunRecord(**{**record.__dict__, "pid": 9877})
    store.write(replacement)
    signaled: list[tuple[int, int]] = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: signaled.append((pid, sig)))

    cli._stop_record(store, record, timeout=0)

    assert signaled == []


def test_serve_explicit_options_override_resolved_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    environment_token = "TTS_STUDIO_TEST_ENVIRONMENT_TOKEN"
    data_dir = tmp_path / "override"
    monkeypatch.setenv("TTS_STUDIO_HOST", "0.0.0.0")
    monkeypatch.setenv("TTS_STUDIO_PORT", "9000")
    monkeypatch.setenv("TTS_STUDIO_DATA_DIR", str(tmp_path / "environment"))
    monkeypatch.setenv("TTS_STUDIO_API_TOKEN_ENV", environment_token)
    monkeypatch.setenv(environment_token, "environment-secret")
    monkeypatch.setattr(
        cli, "create_app", lambda settings: captured.setdefault("settings", settings)
    )
    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda application, host, port: captured.update(
            application=application, host=host, port=port
        ),
    )

    result = runner.invoke(
        app,
        [
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            "7654",
            "--data-dir",
            str(data_dir),
        ],
    )

    assert result.exit_code == 0
    settings = captured["settings"]
    assert isinstance(settings, Settings)
    assert settings.host == "127.0.0.1"
    assert settings.port == 7654
    assert settings.data_dir == data_dir.resolve()
    assert settings.api_token_env == environment_token
    assert captured["application"] is settings
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 7654


def test_serve_can_explicitly_include_test_adapters(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli,
        "create_app",
        lambda settings, *, include_test_adapters=False: captured.update(
            settings=settings, include_test_adapters=include_test_adapters
        ),
    )
    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda application, host, port: captured.update(host=host, port=port),
    )

    result = runner.invoke(
        app,
        ["serve", "--include-test-adapters", "--data-dir", str(tmp_path / "data")],
    )

    assert result.exit_code == 0
    assert captured["include_test_adapters"] is True


@pytest.mark.skipif(sys.platform == "win32", reason="threaded Uvicorn startup is unavailable on Windows CI")
def test_status_prints_public_response(http_server_url: str) -> None:
    result = runner.invoke(app, ["status", "--url", http_server_url])

    assert result.exit_code == 0
    assert "healthy" in result.output
    assert ".tts-studio" in result.output


def test_status_exits_three_when_core_is_unavailable() -> None:
    result = runner.invoke(app, ["status", "--url", "http://127.0.0.1:1"])

    assert result.exit_code == 3
    assert "Unable to reach Core" in result.output


@pytest.mark.parametrize(
    "transport_error",
    [
        httpx.ReadTimeout("response stalled"),
        httpx.TransportError("transport failed"),
    ],
)
def test_core_client_normalizes_transport_failures(
    monkeypatch: pytest.MonkeyPatch,
    transport_error: httpx.TransportError,
) -> None:
    def raise_transport_error(
        client: httpx.Client, method: str, url: str, **kwargs: object
    ) -> httpx.Response:
        del client, method, url, kwargs
        raise transport_error

    monkeypatch.setattr(httpx.Client, "request", raise_transport_error)

    with pytest.raises(CoreUnavailable):
        CoreClient("http://127.0.0.1:7860").system_status()


def test_status_maps_a_stalled_response_to_exit_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_read_timeout(
        client: httpx.Client, method: str, url: str, **kwargs: object
    ) -> httpx.Response:
        del client, method, url, kwargs
        raise httpx.ReadTimeout("response stalled")

    monkeypatch.setattr(httpx.Client, "request", raise_read_timeout)

    result = runner.invoke(app, ["status", "--url", "http://127.0.0.1:7860"])

    assert result.exit_code == 3
    assert "Unable to reach Core" in result.output


@pytest.mark.parametrize("args", (["--help"], ["serve", "--help"], ["status", "--help"]))
def test_help_for_implemented_commands_exits_zero(args: list[str]) -> None:
    result = runner.invoke(app, args)

    assert result.exit_code == 0
