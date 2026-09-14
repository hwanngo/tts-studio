import asyncio
import json
import os
import signal
import stat
import subprocess
import sys
from asyncio.subprocess import Process
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tts_studio_protocol.engine.v1 import engine_pb2

import tts_studio.workers.supervisor as supervisor_module
from tts_studio.storage.layout import StorageLayout
from tts_studio.workers.generation import WorkerCapabilities
from tts_studio.workers.process import WorkerLaunchSpec, WorkerProcess
from tts_studio.workers.supervisor import WorkerSupervisor


def test_worker_capabilities_reject_malformed_metadata() -> None:
    with pytest.raises(ValueError, match="max_concurrency"):
        supervisor_module._worker_capabilities(
            engine_pb2.DescribeResponse(
                engine_id="fake",
                engine_version="1.0.0",
                max_concurrency=0,
            )
        )

    with pytest.raises(ValueError, match="capability name"):
        supervisor_module._worker_capabilities(
            engine_pb2.DescribeResponse(
                engine_id="fake",
                engine_version="1.0.0",
                max_concurrency=1,
                capabilities=[engine_pb2.Capability(name="", supported=True)],
            )
        )

    with pytest.raises(ValueError, match="alignment"):
        supervisor_module._worker_capabilities(
            engine_pb2.DescribeResponse(
                engine_id="fake",
                engine_version="1.0.0",
                max_concurrency=1,
                capabilities=[engine_pb2.Capability(name="alignment", supported=True)],
                alignment=engine_pb2.AlignmentCapability(aligner="aligner"),
            )
        )


def test_worker_protocol_minor_compatibility_is_explicit() -> None:
    assert supervisor_module._protocol_compatible(engine_pb2.ProtocolVersion(major=1, minor=0))
    assert supervisor_module._protocol_compatible(engine_pb2.ProtocolVersion(major=1, minor=1))
    assert not supervisor_module._protocol_compatible(engine_pb2.ProtocolVersion(major=1, minor=2))
    assert not supervisor_module._protocol_compatible(engine_pb2.ProtocolVersion(major=2, minor=0))


def test_pending_owner_lookup_requests_untruncated_process_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_claim = "pending-owner-claim"
    command_prefix = "/home/runner/work/tts-studio/tts-studio/.venv/bin/python"
    process_prefix = "4242 4242 Ss Mon Sep 14 12:00:00 2026 "

    monkeypatch.setattr(supervisor_module.shutil, "which", lambda name: "/usr/bin/ps")
    monkeypatch.setattr(supervisor_module.sys, "platform", "linux")

    def process_table(arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        command = f"{command_prefix} -c 'import time; time.sleep(60)'"
        if "-ww" in arguments:
            command = f"{command} --owner-claim {owner_claim}"
        return subprocess.CompletedProcess(arguments, 0, stdout=f"{process_prefix}{command}\n")

    monkeypatch.setattr(supervisor_module.subprocess, "run", process_table)

    matches = supervisor_module._pending_owner_processes({"owner_claim": owner_claim})

    assert matches is not None
    assert [process.pid for process in matches] == [4242]


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_FAKE_WORKER_LAUNCH = WorkerLaunchSpec(
    command=(
        "uv",
        "run",
        "--project",
        "workers/fake",
        "tts-studio-fake-worker",
    ),
    cwd=_REPOSITORY_ROOT,
)

_READY_FILE_WRITER = """
import argparse
import json
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--published-host", required=True)
parser.add_argument("--published-port", required=True)
parser.add_argument("--host", required=True)
parser.add_argument("--port", required=True)
parser.add_argument("--token-file", required=True, type=Path)
parser.add_argument("--ready-file", required=True, type=Path)
parser.add_argument("--data-dir", required=True, type=Path)
parser.add_argument("--owner-claim")
args = parser.parse_args()
args.ready_file.write_text(
    json.dumps({"host": args.published_host, "port": json.loads(args.published_port)})
)
time.sleep(60)
"""

_PROTOCOL_WORKER = """
import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from tts_studio_protocol.engine.v1 import engine_pb2, engine_pb2_grpc
from tts_studio_worker_sdk.auth import consume_worker_token
from tts_studio_worker_sdk.server import serve_worker

parser = argparse.ArgumentParser()
parser.add_argument("--reported-engine-id", required=True)
parser.add_argument("--protocol-major", required=True, type=int)
parser.add_argument("--probe-file", type=Path)
parser.add_argument("--sentinel-name")
parser.add_argument("--helper-pid-file", type=Path)
parser.add_argument("--host", required=True)
parser.add_argument("--port", required=True, type=int)
parser.add_argument("--token-file", required=True, type=Path)
parser.add_argument("--ready-file", required=True, type=Path)
parser.add_argument("--data-dir", required=True, type=Path)
parser.add_argument("--owner-claim")
args = parser.parse_args()
token = consume_worker_token(args.token_file)
if args.helper_pid_file is not None:
    subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import signal,sys,time; from pathlib import Path; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "Path(sys.argv[1]).write_text(str(__import__('os').getpid())); "
                "time.sleep(60)"
            ),
            str(args.helper_pid_file),
        ]
    )
if args.probe_file is not None:
    args.probe_file.write_text(
        json.dumps(
            {
                "cwd": str(Path.cwd()),
                "sentinel": os.environ.get(args.sentinel_name),
            }
        )
    )

class Worker(engine_pb2_grpc.EngineWorkerServicer):
    async def Describe(self, request, context):
        return engine_pb2.DescribeResponse(
            protocol=engine_pb2.ProtocolVersion(major=args.protocol_major),
            engine_id=args.reported_engine_id,
            engine_version="test",
            max_concurrency=1,
        )

    async def Health(self, request, context):
        return engine_pb2.HealthResponse(status=engine_pb2.HealthResponse.READY)

asyncio.run(
    serve_worker(
        Worker(),
        host=args.host,
        port=args.port,
        token=token,
        ready_file=args.ready_file,
    )
)
"""


@pytest.mark.asyncio
async def test_describe_uses_authenticated_running_worker_and_bounded_deadline(
    tmp_path: Path,
) -> None:
    calls: dict[str, Any] = {}

    class RecordingStub:
        async def Describe(self, request: Any, *, metadata: Any, timeout: float) -> Any:
            calls.update(request=request, metadata=metadata, timeout=timeout)
            return engine_pb2.DescribeResponse(
                protocol=engine_pb2.ProtocolVersion(major=1, minor=0),
                engine_id="fake",
                engine_version="1.0.0",
                max_concurrency=2,
                capabilities=[engine_pb2.Capability(name="preset_voices", supported=True)],
            )

    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=1)
    supervisor._workers["fake"] = cast(
        WorkerProcess,
        SimpleNamespace(stub=RecordingStub(), token="secret-worker-token", engine_id="fake"),
    )

    response = await supervisor.describe("fake")

    assert response.engine_id == "fake"
    assert calls["request"] == engine_pb2.DescribeRequest()
    assert calls["metadata"] == (("x-tts-worker-token", "secret-worker-token"),)
    assert calls["timeout"] == 10.0
    assert "secret-worker-token" not in repr(response)


@pytest.mark.asyncio
async def test_describe_rejects_protocol_and_identity_changes_after_admission(
    tmp_path: Path,
) -> None:
    class RefreshStub:
        def __init__(self) -> None:
            self.response = engine_pb2.DescribeResponse(
                protocol=engine_pb2.ProtocolVersion(major=1, minor=0),
                engine_id="fake",
                engine_version="1.0.0",
                max_concurrency=1,
            )

        async def Describe(self, request: Any, *, metadata: Any, timeout: float) -> Any:
            del request, metadata, timeout
            return self.response

    stub = RefreshStub()
    worker = cast(WorkerProcess, SimpleNamespace(stub=stub, token="secret", engine_id="fake"))
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=1)
    supervisor._workers["fake"] = worker

    stub.response.protocol.minor = 2
    with pytest.raises(RuntimeError, match="protocol minor"):
        await supervisor.describe("fake")

    stub.response.protocol.minor = 0
    stub.response.engine_id = "changed"
    with pytest.raises(RuntimeError, match="engine identity"):
        await supervisor.describe("fake")


@pytest.mark.asyncio
async def test_describe_refresh_rejects_contradictory_alignment_metadata(tmp_path: Path) -> None:
    class RefreshStub:
        async def Describe(self, request: Any, *, metadata: Any, timeout: float) -> Any:
            del request, metadata, timeout
            return engine_pb2.DescribeResponse(
                protocol=engine_pb2.ProtocolVersion(major=1, minor=0),
                engine_id="fake",
                engine_version="1.0.0",
                max_concurrency=1,
                capabilities=[engine_pb2.Capability(name="alignment", supported=False)],
                alignment=engine_pb2.AlignmentCapability(
                    units=["word"], languages=["und"], aligner="aligner"
                ),
            )

    worker = cast(
        WorkerProcess, SimpleNamespace(stub=RefreshStub(), token="secret", engine_id="fake")
    )
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=1)
    supervisor._workers["fake"] = worker

    with pytest.raises(ValueError, match="supported"):
        await supervisor.describe("fake")


@pytest.mark.asyncio
async def test_describe_refreshes_cached_capabilities_after_lazy_load(
    tmp_path: Path,
) -> None:
    class LazyCapabilityStub:
        def __init__(self) -> None:
            self.calls = 0

        async def Describe(self, request: Any, *, metadata: Any, timeout: float) -> Any:
            del request, metadata, timeout
            self.calls += 1
            capabilities = [engine_pb2.Capability(name="preset_voices", supported=True)]
            if self.calls > 1:
                capabilities.append(engine_pb2.Capability(name="reference_cloning", supported=True))
            return engine_pb2.DescribeResponse(
                protocol=engine_pb2.ProtocolVersion(major=1, minor=0),
                engine_id="vieneu",
                engine_version="3.6.3",
                max_concurrency=1,
                capabilities=capabilities,
            )

    stub = LazyCapabilityStub()
    worker = cast(
        WorkerProcess,
        SimpleNamespace(
            stub=stub,
            token="secret-worker-token",
            engine_id="vieneu",
            capabilities=WorkerCapabilities(
                engine_id="vieneu",
                engine_version="3.6.3",
                supported=frozenset({"preset_voices"}),
                max_concurrency=1,
            ),
        ),
    )
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=1)
    supervisor._workers["vieneu"] = worker

    await supervisor.describe("vieneu")
    assert "reference_cloning" not in worker.capabilities.supported

    await supervisor.describe("vieneu")

    assert "reference_cloning" in worker.capabilities.supported


@pytest.fixture
def launched_processes(monkeypatch: pytest.MonkeyPatch) -> list[Process]:
    processes: list[Process] = []
    create_subprocess_exec = asyncio.create_subprocess_exec

    async def record_process(*args: Any, **kwargs: Any) -> Process:
        process = await create_subprocess_exec(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record_process)
    return processes


async def _reap_processes(processes: list[Process]) -> None:
    for process in processes:
        if process.returncode is None:
            process.terminate()
    await asyncio.gather(*(process.wait() for process in processes))


async def _wait_until(predicate: Any) -> None:
    while not predicate():
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_starts_authenticates_and_stops_fake_worker(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)

    worker = await supervisor.start("fake", _FAKE_WORKER_LAUNCH)
    try:
        status = await supervisor.health("fake")

        assert worker.process.returncode is None
        assert "token=" not in repr(worker)
        assert worker.token not in repr(worker)
        assert status.engine_id == "fake"
        assert status.ready is True
    finally:
        await supervisor.stop_all()

    assert worker.process.returncode is not None


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="verified process-group containment is POSIX-only")
async def test_stop_terminates_verified_worker_process_group_descendants(
    tmp_path: Path,
) -> None:
    helper_pid_file = tmp_path / "helper.pid"
    launch = WorkerLaunchSpec(
        command=(
            sys.executable,
            "-c",
            _PROTOCOL_WORKER,
            "--reported-engine-id",
            "fake",
            "--protocol-major",
            "1",
            "--helper-pid-file",
            str(helper_pid_file),
        ),
        cwd=_REPOSITORY_ROOT,
    )
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path / "data"), startup_timeout=10)
    await supervisor.start("fake", launch)
    await asyncio.wait_for(_wait_until(helper_pid_file.exists), timeout=2)
    helper_pid = int(helper_pid_file.read_text(encoding="utf-8"))

    try:
        await supervisor.stop_all()
        await asyncio.wait_for(
            _wait_until(lambda: not supervisor_module._pid_alive(helper_pid)), timeout=2
        )
        assert not supervisor_module._pid_alive(helper_pid)
    finally:
        if supervisor_module._pid_alive(helper_pid):
            os.kill(helper_pid, signal.SIGKILL)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="verified process-group containment is POSIX-only")
async def test_stop_cleans_claimed_descendants_after_worker_leader_exits_first(
    tmp_path: Path,
) -> None:
    helper_pid_file = tmp_path / "leader-first-helper.pid"
    launch = WorkerLaunchSpec(
        command=(
            sys.executable,
            "-c",
            _PROTOCOL_WORKER,
            "--reported-engine-id",
            "fake",
            "--protocol-major",
            "1",
            "--helper-pid-file",
            str(helper_pid_file),
        ),
        cwd=_REPOSITORY_ROOT,
    )
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path / "data"), startup_timeout=10)
    worker = await supervisor.start("fake", launch)
    await asyncio.wait_for(_wait_until(helper_pid_file.exists), timeout=2)
    helper_pid = int(helper_pid_file.read_text(encoding="utf-8"))
    watch = supervisor._watch_tasks.pop(("fake", 0))
    watch.cancel()
    await asyncio.gather(watch, return_exceptions=True)
    worker.process.terminate()
    await worker.process.wait()

    try:
        await supervisor.stop_all()
        await asyncio.wait_for(
            _wait_until(lambda: not supervisor_module._pid_alive(helper_pid)), timeout=2
        )
        assert not supervisor_module._pid_alive(helper_pid)
        assert worker.owner_file is not None
        assert not worker.owner_file.exists()
    finally:
        if supervisor_module._pid_alive(helper_pid):
            os.kill(helper_pid, signal.SIGKILL)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="orphan reconciliation is POSIX-only")
async def test_startup_reconciliation_cleans_claimed_descendants_after_dead_leader(
    tmp_path: Path,
) -> None:
    helper_pid_file = tmp_path / "orphan-helper.pid"
    layout = StorageLayout.from_root(tmp_path / "data")
    launch = WorkerLaunchSpec(
        command=(
            sys.executable,
            "-c",
            _PROTOCOL_WORKER,
            "--reported-engine-id",
            "fake",
            "--protocol-major",
            "1",
            "--helper-pid-file",
            str(helper_pid_file),
        ),
        cwd=_REPOSITORY_ROOT,
    )
    original = WorkerSupervisor(layout, startup_timeout=10)
    worker = await original.start("fake", launch)
    await asyncio.wait_for(_wait_until(helper_pid_file.exists), timeout=2)
    helper_pid = int(helper_pid_file.read_text(encoding="utf-8"))
    owner_file = worker.owner_file
    assert owner_file is not None
    watch = original._watch_tasks.pop(("fake", 0))
    watch.cancel()
    await asyncio.gather(watch, return_exceptions=True)
    worker.process.terminate()
    await worker.process.wait()
    original._workers.clear()
    original._replicas.clear()
    original._launches.clear()

    try:
        await WorkerSupervisor(layout, startup_timeout=1)._reconcile_orphans()
        await asyncio.wait_for(
            _wait_until(lambda: not supervisor_module._pid_alive(helper_pid)), timeout=2
        )
        assert not owner_file.exists()
    finally:
        await worker.channel.close()
        if supervisor_module._pid_alive(helper_pid):
            os.kill(helper_pid, signal.SIGKILL)
        owner_file.unlink(missing_ok=True)
        worker.ready_file.unlink(missing_ok=True)


def test_owner_finalization_failure_preserves_parseable_pending_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    owner = layout.run / "worker-atomic.owner"
    ready = layout.run / "worker-atomic.json"
    token = layout.run / "worker-atomic.token"
    supervisor_module._write_owner_record(owner, None, None, "owner-claim", ready, token)
    pending = json.loads(owner.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        supervisor_module,
        "_process_identity",
        lambda pid: SimpleNamespace(process_group=pid, start_time="start", command_sha256="hash"),
        raising=False,
    )

    def partial_write(descriptor: int, *args: object, **kwargs: object) -> None:
        del args, kwargs
        os.ftruncate(descriptor, 0)
        os.write(descriptor, b"{")
        raise OSError("simulated finalization crash")

    monkeypatch.setattr(supervisor_module, "_write_owner_document", partial_write)

    with pytest.raises(OSError, match="finalization crash"):
        supervisor_module._finalize_owner_record(owner, os.getpid(), "owner-claim", ready, token)

    assert json.loads(owner.read_text(encoding="utf-8")) == pending


@pytest.mark.asyncio
async def test_launch_uses_a_restricted_consumed_token_file_and_explicit_process_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    supervisor = WorkerSupervisor(layout, startup_timeout=10)
    real_create_subprocess_exec = asyncio.create_subprocess_exec
    captured: dict[str, Any] = {}
    sentinel_name = "TTS_STUDIO_PARENT_SECRET_SENTINEL"
    monkeypatch.setenv(sentinel_name, "must-not-reach-worker")
    launch_cwd = tmp_path / "worker-cwd"
    launch_cwd.mkdir()
    probe_file = tmp_path / "child-process.json"
    launch = WorkerLaunchSpec(
        command=(
            sys.executable,
            "-c",
            _PROTOCOL_WORKER,
            "--reported-engine-id",
            "fake",
            "--protocol-major",
            "1",
            "--probe-file",
            str(probe_file),
            "--sentinel-name",
            sentinel_name,
        ),
        cwd=launch_cwd,
    )

    async def capture_launch(*args: str, **kwargs: Any) -> Process:
        token_file = Path(args[args.index("--token-file") + 1])
        owner_files = tuple(layout.run.glob("*.owner"))
        assert len(owner_files) == (0 if os.name == "nt" else 1)
        captured.update(
            arguments=args,
            cwd=kwargs.get("cwd"),
            environment=kwargs.get("env"),
            token_file=token_file,
            token_content=token_file.read_text(encoding="utf-8"),
            token_mode=stat.S_IMODE(token_file.stat().st_mode),
            owner_file=owner_files[0] if owner_files else None,
            pending_owner=(
                json.loads(owner_files[0].read_text(encoding="utf-8")) if owner_files else None
            ),
            start_new_session=kwargs.get("start_new_session", False),
            creationflags=kwargs.get("creationflags", 0),
        )
        return await real_create_subprocess_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_launch)

    worker = await supervisor.start("fake", launch)
    try:
        arguments = captured["arguments"]
        environment = captured["environment"]
        token_file = captured["token_file"]
        assert isinstance(arguments, tuple)
        assert isinstance(environment, dict)
        assert isinstance(token_file, Path)
        assert "--token-file" in arguments
        assert arguments[arguments.index("--data-dir") + 1] == str(layout.root)
        assert "--data-dir" in arguments
        assert "--owner-claim" in arguments
        assert arguments[arguments.index("--owner-claim") + 1]
        assert captured["start_new_session"] is (os.name != "nt")
        assert captured["creationflags"] == 0
        assert "--token" not in arguments
        assert all(worker.token not in argument for argument in arguments)
        assert captured["token_content"] == worker.token
        assert captured["token_mode"] == 0o600
        if os.name == "nt":
            assert worker.owner_file is None
            assert captured["owner_file"] is None
            assert captured["pending_owner"] is None
        else:
            pending_owner = captured["pending_owner"]
            assert isinstance(pending_owner, dict)
            assert worker.owner_file is not None
            assert pending_owner["pid"] is None
            assert pending_owner["process_group"] is None
            assert pending_owner["owner_claim"] == arguments[arguments.index("--owner-claim") + 1]
        assert token_file.parent == layout.run
        assert not token_file.exists()
        assert sentinel_name not in environment
        assert (
            environment[supervisor_module._OWNER_CLAIM_ENV]
            == arguments[arguments.index("--owner-claim") + 1]
        )
        assert Path(captured["cwd"]) == launch_cwd.resolve()
        child_process = json.loads(probe_file.read_text(encoding="utf-8"))
        assert child_process == {"cwd": str(launch_cwd.resolve()), "sentinel": None}
    finally:
        await supervisor.stop_all()

    assert not Path(captured["token_file"]).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("child_name", ["logs", "run"])
async def test_launch_rejects_redirected_managed_directories_without_outside_files(
    tmp_path: Path,
    launched_processes: list[Process],
    child_name: str,
) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    outside = tmp_path / f"outside-{child_name}"
    outside.mkdir()
    managed_child = getattr(layout, child_name)
    managed_child.rmdir()
    managed_child.symlink_to(outside, target_is_directory=True)
    supervisor = WorkerSupervisor(layout, startup_timeout=1)

    with pytest.raises(RuntimeError, match=rf"managed directory .*{child_name}.*unsafe"):
        await supervisor.start("redirected", _FAKE_WORKER_LAUNCH)

    assert launched_processes == []
    assert tuple(outside.iterdir()) == ()


@pytest.mark.asyncio
async def test_spawn_failure_removes_the_launch_token_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")

    async def fail_to_spawn(*args: str, **kwargs: Any) -> Process:
        del args, kwargs
        raise OSError("spawn failed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_to_spawn)
    supervisor = WorkerSupervisor(layout, startup_timeout=1)

    with pytest.raises(OSError, match="spawn failed"):
        await supervisor.start("fake", _FAKE_WORKER_LAUNCH)

    assert tuple(layout.run.glob("*.token")) == ()


@pytest.mark.asyncio
async def test_starts_worker_when_launch_token_starts_with_a_dash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supervisor_module.secrets, "token_urlsafe", lambda _: "-worker-token")
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)

    worker = await supervisor.start("fake", _FAKE_WORKER_LAUNCH)
    try:
        assert (await supervisor.health("fake")).ready is True
    finally:
        await supervisor.stop_all()

    assert worker.process.returncode is not None


@pytest.mark.asyncio
async def test_statuses_returns_an_immutable_engine_ordered_snapshot(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)
    alpha_launch = WorkerLaunchSpec(
        command=(
            sys.executable,
            "-c",
            _PROTOCOL_WORKER,
            "--reported-engine-id",
            "alpha",
            "--protocol-major",
            "1",
        ),
        cwd=_REPOSITORY_ROOT,
    )
    zeta_launch = WorkerLaunchSpec(
        command=(
            sys.executable,
            "-c",
            _PROTOCOL_WORKER,
            "--reported-engine-id",
            "zeta",
            "--protocol-major",
            "1",
        ),
        cwd=_REPOSITORY_ROOT,
    )

    try:
        await supervisor.start("zeta", zeta_launch)
        await supervisor.start("alpha", alpha_launch)

        statuses = await supervisor.statuses()

        assert isinstance(statuses, tuple)
        assert tuple(status.engine_id for status in statuses) == ("alpha", "zeta")
        assert all(status.ready for status in statuses)
    finally:
        await supervisor.stop_all()


@pytest.mark.asyncio
async def test_engine_identity_mismatch_reaps_worker(
    tmp_path: Path, launched_processes: list[Process]
) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)

    try:
        with pytest.raises(RuntimeError, match="engine identity"):
            await supervisor.start("not-fake", _FAKE_WORKER_LAUNCH)

        assert len(launched_processes) == 1
        assert launched_processes[0].returncode is not None
    finally:
        await supervisor.stop_all()
        await _reap_processes(launched_processes)


@pytest.mark.asyncio
async def test_missing_ready_file_times_out_and_reaps_worker(
    tmp_path: Path, launched_processes: list[Process]
) -> None:
    layout = StorageLayout.from_root(tmp_path)
    supervisor = WorkerSupervisor(layout, startup_timeout=0.05)
    launch = WorkerLaunchSpec(
        command=(sys.executable, "-c", "import time; time.sleep(60)"),
        cwd=_REPOSITORY_ROOT,
    )

    try:
        with pytest.raises(TimeoutError, match="did not become ready"):
            await supervisor.start("never-ready", launch)

        assert len(launched_processes) == 1
        assert launched_processes[0].returncode is not None
        assert tuple(layout.run.glob("*.token")) == ()
    finally:
        await supervisor.stop_all()
        await _reap_processes(launched_processes)


@pytest.mark.asyncio
async def test_wait_for_ready_file_retries_a_transient_open_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready_file = tmp_path / "ready.json"
    ready_file.write_text(json.dumps({"host": "127.0.0.1", "port": 5000}))
    original_open = supervisor_module.os.open
    open_attempts = 0

    def open_with_initial_miss(path: str | os.PathLike[str], flags: int, *args: Any) -> int:
        nonlocal open_attempts
        if Path(path) == ready_file and open_attempts == 0:
            open_attempts += 1
            raise FileNotFoundError
        return original_open(path, flags, *args)

    monkeypatch.setattr(supervisor_module.os, "open", open_with_initial_miss)
    loop = asyncio.get_running_loop()

    await supervisor_module._wait_for_ready_file(
        "fake",
        cast(Process, SimpleNamespace(returncode=None)),
        ready_file,
        loop.time() + 1,
    )

    assert open_attempts == 1


@pytest.mark.asyncio
async def test_hung_channel_health_probe_is_bounded(tmp_path: Path) -> None:
    class HungStub:
        async def Health(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            await asyncio.sleep(60)

    worker = cast(
        WorkerProcess,
        SimpleNamespace(stub=HungStub(), token="secret", process=SimpleNamespace(pid=42)),
    )
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=1)

    status = await asyncio.wait_for(supervisor._health_replica("fake", 0, worker), timeout=2)

    assert status.ready is False
    assert "TimeoutError" in status.message


@pytest.mark.asyncio
async def test_channel_health_failure_is_reported_unhealthy(tmp_path: Path) -> None:
    class BrokenStub:
        async def Health(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            raise RuntimeError("channel unavailable token=secret")

    worker = cast(
        WorkerProcess,
        SimpleNamespace(stub=BrokenStub(), token="secret", process=SimpleNamespace(pid=42)),
    )
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=1)

    status = await supervisor._health_replica("fake", 0, worker)

    assert status.ready is False
    assert "secret" not in status.message


@pytest.mark.asyncio
async def test_missing_worker_health_is_not_ready(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=1)

    status = await supervisor.health("missing")

    assert status.engine_id == "missing"
    assert status.ready is False
    assert status.pid is None


@pytest.mark.asyncio
async def test_noncanonical_replica_crash_is_visible_and_restarted(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)
    launch = WorkerLaunchSpec(
        command=(
            sys.executable,
            "-c",
            _PROTOCOL_WORKER,
            "--reported-engine-id",
            "fake",
            "--protocol-major",
            "1",
        ),
        cwd=_REPOSITORY_ROOT,
    )
    try:
        await supervisor.start("fake", launch)
        model = SimpleNamespace(
            compatibility_evidence={"engine_id": "fake"},
            engine_installation_id="fake@revision",
            desired_replicas=2,
        )
        await supervisor.ensure_replicas(model, 2)
        crashed = supervisor._replicas["fake"][1]
        crashed_pid = crashed.process.pid
        crashed.process.terminate()
        await asyncio.wait_for(
            _wait_until(
                lambda: (
                    len(supervisor._replicas.get("fake", {})) == 2
                    and supervisor._replicas["fake"][1].process.pid != crashed_pid
                    and supervisor.startup_diagnostics()["fake"]["restart_count"] >= 1
                )
            ),
            timeout=5,
        )
        assert supervisor._replicas["fake"][1].process.pid != crashed_pid
        assert supervisor.startup_diagnostics()["fake"]["restart_count"] >= 1
        assert "last_failure" in supervisor.startup_diagnostics()["fake"]
    finally:
        await supervisor.stop_all()


@pytest.mark.asyncio
async def test_crash_replacement_retries_failed_launches_until_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)
    monkeypatch.setattr(supervisor_module, "_RESTART_BASE_DELAY_SECONDS", 0.001)
    monkeypatch.setattr(supervisor_module, "_RESTART_MAX_DELAY_SECONDS", 0.002)
    worker = await supervisor.start("fake", _FAKE_WORKER_LAUNCH)
    original_start = supervisor._start_locked
    replacement_attempts = 0

    async def flaky_start(
        engine_id: str, launch: WorkerLaunchSpec, replica_id: int = 0
    ) -> WorkerProcess:
        nonlocal replacement_attempts
        replacement_attempts += 1
        if replacement_attempts < 3:
            raise OSError("replacement launch failed")
        return await original_start(engine_id, launch, replica_id)

    monkeypatch.setattr(supervisor, "_start_locked", flaky_start)
    crashed_pid = worker.process.pid
    worker.process.terminate()
    try:
        await asyncio.wait_for(
            _wait_until(
                lambda: (
                    0 in supervisor._replicas.get("fake", {})
                    and supervisor._replicas["fake"][0].process.pid != crashed_pid
                )
            ),
            timeout=5,
        )
        assert replacement_attempts == 3
        assert supervisor.startup_diagnostics()["fake"]["status"] == "ready"
        assert supervisor.startup_diagnostics()["fake"]["restart_count"] == 3
    finally:
        await supervisor.stop_all()


@pytest.mark.asyncio
async def test_crash_replacement_exhausts_three_consecutive_launch_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)
    monkeypatch.setattr(supervisor_module, "_RESTART_BASE_DELAY_SECONDS", 0.001)
    monkeypatch.setattr(supervisor_module, "_RESTART_MAX_DELAY_SECONDS", 0.002)
    worker = await supervisor.start("fake", _FAKE_WORKER_LAUNCH)
    replacement_attempts = 0

    async def fail_start(
        engine_id: str, launch: WorkerLaunchSpec, replica_id: int = 0
    ) -> WorkerProcess:
        nonlocal replacement_attempts
        del engine_id, launch, replica_id
        replacement_attempts += 1
        raise OSError("replacement launch failed")

    monkeypatch.setattr(supervisor, "_start_locked", fail_start)
    worker.process.terminate()
    try:
        await asyncio.wait_for(
            _wait_until(
                lambda: supervisor.startup_diagnostics().get("fake", {}).get("status") == "failed"
            ),
            timeout=5,
        )
        assert replacement_attempts == 3
        assert supervisor.startup_diagnostics()["fake"]["restart_count"] == 3
        assert supervisor._replicas.get("fake", {}) == {}
    finally:
        await supervisor.stop_all()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="orphan reconciliation is POSIX-only")
async def test_startup_reconciliation_terminates_live_verified_orphan(
    tmp_path: Path,
) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    original = WorkerSupervisor(layout, startup_timeout=10)
    worker = await original.start("fake", _FAKE_WORKER_LAUNCH)
    owner_file = worker.owner_file
    assert owner_file is not None
    owner_record = json.loads(owner_file.read_text(encoding="utf-8"))
    assert owner_record["owner_claim"]
    assert owner_record["process_group"] == worker.process.pid

    watch = original._watch_tasks.pop(("fake", 0))
    watch.cancel()
    await asyncio.gather(watch, return_exceptions=True)
    original._workers.clear()
    original._replicas.clear()
    original._launches.clear()

    replacement = WorkerSupervisor(layout, startup_timeout=10)
    try:
        await replacement._reconcile_orphans()
        await asyncio.wait_for(worker.process.wait(), timeout=2)
        assert worker.process.returncode is not None
        assert not owner_file.exists()
    finally:
        await worker.channel.close()
        if worker.process.returncode is None:
            worker.process.terminate()
            await worker.process.wait()
        owner_file.unlink(missing_ok=True)
        worker.ready_file.unlink(missing_ok=True)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="orphan reconciliation is POSIX-only")
async def test_startup_reconciliation_terminates_pending_owner_spawn_window(
    tmp_path: Path,
) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    owner_claim = "pending-owner-claim"
    process = await asyncio.to_thread(
        subprocess.Popen,
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            "--owner-claim",
            owner_claim,
        ],
        start_new_session=True,
        env={**os.environ, supervisor_module._OWNER_CLAIM_ENV: owner_claim},
    )
    owner = layout.run / "worker-pending.owner"
    ready = layout.run / "worker-pending.json"
    token = layout.run / "worker-pending.token"
    owner.write_text(
        json.dumps(
            {
                "pid": None,
                "process_group": None,
                "owner_claim": owner_claim,
                "ready": ready.name,
                "token": token.name,
            }
        ),
        encoding="utf-8",
    )
    ready.write_text("{}", encoding="utf-8")
    token.write_text("token", encoding="utf-8")

    try:
        await WorkerSupervisor(layout, startup_timeout=1)._reconcile_orphans()
        # The reconciliation contract is that the orphan has exited. Polling
        # avoids depending on an unrelated default-executor worker to reap it.
        await asyncio.wait_for(_wait_until(lambda: process.poll() is not None), timeout=5)
        assert process.returncode is not None
        assert not owner.exists()
        assert not ready.exists()
        assert not token.exists()
    finally:
        if process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="orphan reconciliation is POSIX-only")
async def test_orphan_termination_revalidates_identity_immediately_before_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    owner = layout.run / "worker-raced.owner"
    owner.write_text(
        json.dumps(
            {
                "pid": 4242,
                "process_group": 4242,
                "owner_claim": "recorded-claim",
                "ready": "worker-raced.json",
                "token": "worker-raced.token",
            }
        ),
        encoding="utf-8",
    )
    expected = supervisor_module._GroupSnapshot(
        4242,
        (
            supervisor_module._ProcessIdentity(
                4242, 4242, "S", "start-one", "worker --owner-claim recorded-claim"
            ),
        ),
    )
    changed = supervisor_module._GroupSnapshot(
        4242,
        (supervisor_module._ProcessIdentity(4242, 4242, "S", "start-two", "unrelated process"),),
    )
    snapshots = iter((expected, changed))
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        supervisor_module,
        "_verified_group_snapshot",
        lambda record: next(snapshots),
    )
    monkeypatch.setattr(
        supervisor_module.os,
        "killpg",
        lambda process_group, signum: signals.append((process_group, signum)),
    )

    await WorkerSupervisor(layout, startup_timeout=1)._reconcile_orphans()

    assert signals == []
    assert owner.exists()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="orphan reconciliation is POSIX-only")
async def test_startup_reconciliation_never_terminates_unrelated_live_pid(
    tmp_path: Path,
) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        "--owner-claim",
        "different-claim",
        start_new_session=os.name != "nt",
    )
    owner = layout.run / "worker-unrelated.owner"
    owner.write_text(
        json.dumps(
            {
                "pid": process.pid,
                "process_group": process.pid,
                "owner_claim": "recorded-claim",
                "ready": "worker-unrelated.json",
                "token": "worker-unrelated.token",
            }
        ),
        encoding="utf-8",
    )

    try:
        await WorkerSupervisor(layout, startup_timeout=1)._reconcile_orphans()
        assert process.returncode is None
        assert owner.exists()
    finally:
        if process.returncode is None:
            process.terminate()
        await process.wait()


@pytest.mark.asyncio
async def test_replica_start_failure_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=1)
    supervisor._launches["fake"] = _FAKE_WORKER_LAUNCH

    async def fail_start(*args: Any, **kwargs: Any) -> WorkerProcess:
        del args, kwargs
        raise RuntimeError("adapter token=secret at /private/model")

    monkeypatch.setattr(supervisor, "_start_locked", fail_start)
    model = SimpleNamespace(
        compatibility_evidence={"engine_id": "fake"}, engine_installation_id="fake@revision"
    )

    with pytest.raises(RuntimeError, match="adapter"):
        await supervisor.ensure_replicas(model, 2)

    diagnostic = supervisor.startup_diagnostics()["fake"]
    assert diagnostic["status"] == "failed"
    assert "secret" not in str(diagnostic)
    assert "/private/model" not in str(diagnostic)


@pytest.mark.asyncio
async def test_duplicate_engine_id_is_rejected_before_launching_another_process(
    tmp_path: Path, launched_processes: list[Process]
) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)

    try:
        await supervisor.start("fake", _FAKE_WORKER_LAUNCH)

        with pytest.raises(ValueError, match="already running"):
            await supervisor.start("fake", _FAKE_WORKER_LAUNCH)

        assert len(launched_processes) == 1
    finally:
        await supervisor.stop_all()
        await _reap_processes(launched_processes)


@pytest.mark.asyncio
async def test_unsupported_protocol_major_reaps_worker(
    tmp_path: Path, launched_processes: list[Process]
) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)
    launch = WorkerLaunchSpec(
        command=(
            sys.executable,
            "-c",
            _PROTOCOL_WORKER,
            "--reported-engine-id",
            "fake",
            "--protocol-major",
            "2",
        ),
        cwd=_REPOSITORY_ROOT,
    )

    try:
        with pytest.raises(RuntimeError, match="unsupported protocol major 2"):
            await supervisor.start("fake", launch)

        assert len(launched_processes) == 1
        assert launched_processes[0].returncode is not None
    finally:
        await supervisor.stop_all()
        await _reap_processes(launched_processes)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="orphan reconciliation is POSIX-only")
async def test_reconcile_orphan_files_only_when_recorded_pid_is_dead(tmp_path: Path) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    owner = layout.run / "worker-fake.owner"
    ready = layout.run / "worker-fake.json"
    token = layout.run / "worker-fake.token"
    owner.write_text(
        json.dumps(
            {
                "pid": 99999999,
                "process_group": 99999999,
                "process_start": "dead",
                "command_sha256": "dead",
                "owner_claim": "dead-owner",
                "ready": ready.name,
                "token": token.name,
            }
        )
    )
    ready.write_text("{}")
    token.write_text("token")
    supervisor = WorkerSupervisor(layout, startup_timeout=1)

    await supervisor._reconcile_orphans()

    assert not owner.exists()
    assert not ready.exists()
    assert not token.exists()


def test_readiness_rejects_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"host": "127.0.0.1", "port": 5000}))
    ready = tmp_path / "ready.json"
    ready.symlink_to(outside)

    with pytest.raises(RuntimeError, match="regular file"):
        supervisor_module._read_readiness(ready)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("host", "port", "message"),
    [
        ("0.0.0.0", 5000, "loopback IP address"),
        ("localhost", 5000, "loopback IP address"),
        ("127.0.0.1", True, "integer between 1 and 65535"),
        ("127.0.0.1", 0, "integer between 1 and 65535"),
    ],
)
async def test_invalid_ready_file_reaps_worker(
    tmp_path: Path,
    launched_processes: list[Process],
    host: str,
    port: object,
    message: str,
) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=1)
    launch = WorkerLaunchSpec(
        command=(
            sys.executable,
            "-c",
            _READY_FILE_WRITER,
            "--published-host",
            host,
            "--published-port",
            json.dumps(port),
        ),
        cwd=_REPOSITORY_ROOT,
    )

    try:
        with pytest.raises(ValueError, match=message):
            await supervisor.start("invalid-ready-file", launch)

        assert len(launched_processes) == 1
        assert launched_processes[0].returncode is not None
    finally:
        await supervisor.stop_all()
        await _reap_processes(launched_processes)


@pytest.mark.asyncio
async def test_stop_during_start_reaps_worker_before_stop_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    describe_entered = asyncio.Event()
    release_describe = asyncio.Event()
    stop_called = asyncio.Event()
    real_stub_type = supervisor_module.engine_pb2_grpc.EngineWorkerStub

    class BlockingDescribeStub:
        def __init__(self, channel: Any) -> None:
            self._delegate = real_stub_type(channel)

        async def Describe(self, *args: Any, **kwargs: Any) -> Any:
            describe_entered.set()
            await release_describe.wait()
            return await self._delegate.Describe(*args, **kwargs)

        async def Health(self, *args: Any, **kwargs: Any) -> Any:
            return await self._delegate.Health(*args, **kwargs)

    monkeypatch.setattr(supervisor_module.engine_pb2_grpc, "EngineWorkerStub", BlockingDescribeStub)
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)
    start_task = asyncio.create_task(supervisor.start("fake", _FAKE_WORKER_LAUNCH))

    async def stop_after_signal() -> None:
        stop_called.set()
        await supervisor.stop_all()

    try:
        await asyncio.wait_for(describe_entered.wait(), timeout=10)
        stop_task = asyncio.create_task(stop_after_signal())
        await stop_called.wait()
        release_describe.set()

        worker = await start_task
        await stop_task

        assert worker.process.returncode is not None
        assert (await supervisor.health("fake")).ready is False
    finally:
        release_describe.set()
        if not start_task.done():
            start_task.cancel()
        await asyncio.gather(start_task, return_exceptions=True)
        await supervisor.stop_all()


@pytest.mark.asyncio
async def test_stop_reaps_worker_when_channel_close_raises(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)
    worker = await supervisor.start("fake", _FAKE_WORKER_LAUNCH)
    real_channel = worker.channel

    class CloseFailureChannel:
        async def close(self) -> None:
            await real_channel.close()
            raise RuntimeError("close failed")

    worker.channel = CloseFailureChannel()  # type: ignore[assignment]

    try:
        with pytest.raises(RuntimeError, match="close failed"):
            await supervisor.stop_all()

        assert worker.process.returncode is not None
        assert (await supervisor.health("fake")).ready is False
    finally:
        await real_channel.close()
        await _reap_processes([worker.process])
        await supervisor.stop_all()


@pytest.mark.asyncio
async def test_hard_stop_recognizes_reaping_after_watcher_close_is_cancelled(
    tmp_path: Path,
) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)
    original = await supervisor.start("fake", _FAKE_WORKER_LAUNCH)
    real_channel = original.channel
    close_entered = asyncio.Event()

    class CancelledCloseChannel:
        first_close = True

        async def close(self) -> None:
            if self.first_close:
                self.first_close = False
                close_entered.set()
                await asyncio.Event().wait()
            await real_channel.close()

    original.channel = CancelledCloseChannel()  # type: ignore[assignment]
    original.process.kill()
    try:
        await asyncio.wait_for(close_entered.wait(), 2)
        await asyncio.wait_for(supervisor.hard_stop(original), 5)
        assert original.terminated
        assert original.owner_file is None or not original.owner_file.exists()
        async with asyncio.timeout(5):
            while not (status := await supervisor.health("fake")).ready:
                await asyncio.sleep(0.01)
        assert status.pid != original.process.pid
        assert supervisor.startup_diagnostics()["fake"]["status"] == "ready"
    finally:
        original.channel = real_channel
        await real_channel.close()
        # The RED implementation can lose the owner file despite a verified exit.
        if (
            original.process.returncode is not None
            and original.owner_file is not None
            and not original.owner_file.exists()
        ):
            original.owner_file = None
        await supervisor.stop_all()


@pytest.mark.asyncio
async def test_concurrent_hard_stop_completion_cannot_replace_new_worker_watcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)
    original = await supervisor.start("fake", _FAKE_WORKER_LAUNCH)
    terminate = supervisor_module._terminate_and_reap
    first_entered, second_entered, release_first, release_second = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    calls = 0

    async def controlled_termination(process, owner_file=None, *, force=False):
        nonlocal calls
        if process is original.process and force:
            calls += 1
            if calls == 1:
                first_entered.set()
                await release_first.wait()
                await terminate(process, owner_file, force=True)
            else:
                second_entered.set()
                await release_second.wait()
            return
        await terminate(process, owner_file, force=force)

    monkeypatch.setattr(supervisor_module, "_terminate_and_reap", controlled_termination)
    first = asyncio.create_task(supervisor.hard_stop(original))
    await asyncio.wait_for(first_entered.wait(), 2)
    second = asyncio.create_task(supervisor.hard_stop(original))
    try:
        # Give the second caller a deterministic chance to reach the delayed
        # termination seam; the correct implementation shares the first task.
        await asyncio.sleep(0.02)
        release_first.set()
        await asyncio.wait_for(first, 5)
        async with asyncio.timeout(5):
            while not (status := await supervisor.health("fake")).ready:
                await asyncio.sleep(0.01)
        assert status.pid != original.process.pid
        replacement_watcher = supervisor._watch_tasks[("fake", 0)]
        release_second.set()
        await asyncio.wait_for(second, 5)
        assert supervisor._watch_tasks[("fake", 0)] is replacement_watcher
        assert not replacement_watcher.done()
        assert not second_entered.is_set(), "concurrent callers must share one termination"
    finally:
        release_first.set()
        release_second.set()
        await asyncio.gather(first, second, return_exceptions=True)
        monkeypatch.setattr(supervisor_module, "_terminate_and_reap", terminate)
        await supervisor.stop_all()


@pytest.mark.asyncio
async def test_repeated_hard_stop_cannot_quarantine_a_replacement(tmp_path: Path) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)
    original = await supervisor.start("fake", _FAKE_WORKER_LAUNCH)
    try:
        await supervisor.hard_stop(original)
        async with asyncio.timeout(5):
            while not (status := await supervisor.health("fake")).ready:
                await asyncio.sleep(0.01)
        assert status.pid != original.process.pid
        await supervisor.hard_stop(original)
        assert (await supervisor.health("fake")).ready
    finally:
        await supervisor.stop_all()


@pytest.mark.asyncio
async def test_planned_stop_retains_ownership_during_failure_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)
    worker = await supervisor.start("fake", _FAKE_WORKER_LAUNCH)
    entered = asyncio.Event()
    cleanup = supervisor_module._cleanup_resources

    async def delayed_cleanup(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(supervisor_module, "_cleanup_resources", delayed_cleanup)
    worker.process.kill()
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await supervisor.stop_all()
        assert not worker.ready_file.exists(), "shutdown must own replicas still being cleaned"
        assert worker.owner_file is None or not worker.owner_file.exists()
    finally:
        if worker.ready_file.exists():
            await cleanup(
                supervisor._layout,
                worker.channel,
                worker.process,
                worker.ready_file,
                worker.token_file,
                worker.owner_file,
            )


@pytest.mark.asyncio
async def test_failed_hard_stop_keeps_quarantine_and_can_be_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)
    worker = await supervisor.start("fake", _FAKE_WORKER_LAUNCH)
    terminate = supervisor_module._terminate_and_reap

    async def denied(*args, **kwargs):
        raise OSError("signal denied")

    monkeypatch.setattr(supervisor_module, "_terminate_and_reap", denied)
    try:
        with pytest.raises(OSError, match="signal denied"):
            await supervisor.hard_stop(worker)
        assert (await supervisor.health("fake")).ready is False
        assert worker.process.returncode is None
        assert worker in supervisor._replica_values()
        with pytest.raises(OSError, match="signal denied"):
            await supervisor.stop_all()
        assert worker in supervisor._replica_values(), "failed reaping must retain ownership"
    finally:
        monkeypatch.setattr(supervisor_module, "_terminate_and_reap", terminate)
        # The direct fallback makes this RED test safe even with the ownership bug.
        await terminate(worker.process, worker.owner_file, force=True)
        await supervisor.stop_all()


@pytest.mark.asyncio
async def test_startup_cleanup_reaps_after_kill_process_lookup_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class KillRaceProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.pid = 1234
            self._killed = asyncio.Event()
            self.wait_completed = False

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            self.returncode = -9
            self._killed.set()
            raise ProcessLookupError

        async def wait(self) -> int:
            await self._killed.wait()
            self.wait_completed = True
            assert self.returncode is not None
            return self.returncode

    process = KillRaceProcess()

    async def launch(*args: Any, **kwargs: Any) -> Any:
        del kwargs
        ready_file = Path(args[args.index("--ready-file") + 1])
        ready_file.write_text(json.dumps({"host": "0.0.0.0", "port": 5000}))
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(supervisor_module, "_SHUTDOWN_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(
        supervisor_module,
        "_process_identity",
        lambda pid: supervisor_module._ProcessIdentity(pid, pid, "", "start", "worker"),
    )
    record = {
        "pid": process.pid,
        "process_group": process.pid,
        "process_start": "start",
        "command_sha256": "hash",
        "owner_claim": "claim",
    }
    snapshot = supervisor_module._GroupSnapshot(
        process.pid,
        (supervisor_module._ProcessIdentity(process.pid, process.pid, "", "start", "worker"),),
    )
    empty_snapshot = supervisor_module._GroupSnapshot(process.pid, ())
    snapshots = iter((snapshot, empty_snapshot))
    monkeypatch.setattr(
        supervisor_module,
        "_verified_process_group",
        lambda pid, owner_file: (record, snapshot),
    )
    monkeypatch.setattr(
        supervisor_module,
        "_verified_group_snapshot",
        lambda current_record: next(snapshots),
    )

    def signal_group(
        current_record: dict[str, object],
        expected: object,
        signum: int,
    ) -> bool:
        del current_record, expected
        if signum == signal.SIGKILL:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        return True

    monkeypatch.setattr(supervisor_module, "_signal_verified_group", signal_group)
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=1)

    with pytest.raises(ValueError, match="loopback IP address"):
        await supervisor.start(
            "kill-race",
            WorkerLaunchSpec(command=("worker-command",), cwd=_REPOSITORY_ROOT),
        )

    assert process.returncode == -9
    assert process.wait_completed is True


@pytest.mark.asyncio
async def test_repeated_stop_cancellation_waits_for_reap(
    tmp_path: Path,
) -> None:
    supervisor = WorkerSupervisor(StorageLayout.from_root(tmp_path), startup_timeout=10)
    worker = await supervisor.start("fake", _FAKE_WORKER_LAUNCH)
    real_channel = worker.channel
    close_entered = asyncio.Event()
    release_close = asyncio.Event()

    class BlockingCloseChannel:
        async def close(self) -> None:
            close_entered.set()
            await release_close.wait()
            await real_channel.close()

    worker.channel = BlockingCloseChannel()  # type: ignore[assignment]
    stop_task = asyncio.create_task(supervisor.stop_all())

    try:
        await asyncio.wait_for(close_entered.wait(), timeout=10)
        stop_task.cancel()
        await asyncio.sleep(0)
        stop_task.cancel()
        release_close.set()

        with pytest.raises(asyncio.CancelledError):
            await stop_task

        assert worker.process.returncode is not None
        assert (await supervisor.health("fake")).ready is False
    finally:
        release_close.set()
        await real_channel.close()
        await _reap_processes([worker.process])
        await supervisor.stop_all()
