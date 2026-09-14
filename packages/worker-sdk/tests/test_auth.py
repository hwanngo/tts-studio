import asyncio
import json
from pathlib import Path
from stat import S_IMODE

import grpc
import pytest
import tts_studio_worker_sdk.auth as auth_module
from tts_studio_protocol.engine.v1 import engine_pb2, engine_pb2_grpc
from tts_studio_worker_sdk.auth import consume_worker_token, require_worker_token
from tts_studio_worker_sdk.server import serve_worker


class Context:
    def __init__(self, metadata: list[tuple[str, str]]) -> None:
        self._metadata = metadata

    def invocation_metadata(self) -> list[tuple[str, str]]:
        return self._metadata

    async def abort(self, code: grpc.StatusCode, details: str) -> None:
        raise RuntimeError((code, details))


@pytest.mark.asyncio
async def test_rejects_missing_token() -> None:
    with pytest.raises(RuntimeError) as error:
        await require_worker_token(Context([]), "secret")

    assert error.value.args[0][0] == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_accepts_matching_token() -> None:
    await require_worker_token(Context([("x-tts-worker-token", "secret")]), "secret")


def test_consumes_a_private_launch_token_file(tmp_path: Path) -> None:
    token_file = tmp_path / "worker.token"
    token_file.write_text("launch-secret", encoding="utf-8")
    token_file.chmod(0o600)

    token = consume_worker_token(token_file)

    assert token == "launch-secret"
    assert not token_file.exists()


def test_rejects_and_removes_a_launch_token_file_with_unsafe_permissions(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "worker.token"
    token_file.write_text("launch-secret", encoding="utf-8")
    token_file.chmod(0o644)
    assert S_IMODE(token_file.stat().st_mode) == 0o644

    with pytest.raises(ValueError, match="private regular file"):
        consume_worker_token(token_file)

    assert not token_file.exists()


def test_accepts_windows_writable_file_mode_under_windows_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "worker.token"
    token_file.write_text("launch-secret", encoding="utf-8")
    token_file.chmod(0o666)
    assert S_IMODE(token_file.stat().st_mode) == 0o666
    monkeypatch.setattr(auth_module, "_WINDOWS", True, raising=False)

    token = consume_worker_token(token_file)

    assert token == "launch-secret"
    assert not token_file.exists()


def test_rejects_windows_writable_file_mode_under_posix_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "worker.token"
    token_file.write_text("launch-secret", encoding="utf-8")
    token_file.chmod(0o666)
    assert S_IMODE(token_file.stat().st_mode) == 0o666
    monkeypatch.setattr(auth_module, "_WINDOWS", False, raising=False)

    with pytest.raises(ValueError, match="private regular file"):
        consume_worker_token(token_file)

    assert not token_file.exists()


@pytest.mark.asyncio
async def test_refuses_a_non_loopback_host(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback"):
        await serve_worker(object(), "0.0.0.0", 0, "secret", tmp_path / "ready.json")


@pytest.mark.asyncio
async def test_publishes_bound_port_and_removes_readiness_file(tmp_path: Path) -> None:
    ready_file = tmp_path / "run" / "worker-ready.json"
    worker = asyncio.create_task(serve_worker(TestServicer(), "127.0.0.1", 0, "secret", ready_file))

    async with asyncio.timeout(1):
        while not ready_file.exists():
            await asyncio.sleep(0.01)

    readiness = json.loads(ready_file.read_text())
    assert readiness["host"] == "127.0.0.1"
    assert readiness["port"] > 0

    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker

    assert not ready_file.exists()


@pytest.mark.asyncio
async def test_rejects_unauthenticated_rpc(tmp_path: Path) -> None:
    ready_file = tmp_path / "worker-ready.json"
    worker = asyncio.create_task(serve_worker(TestServicer(), "127.0.0.1", 0, "secret", ready_file))

    async with asyncio.timeout(1):
        while not ready_file.exists():
            await asyncio.sleep(0.01)

    readiness = json.loads(ready_file.read_text())
    channel = grpc.aio.insecure_channel(f"{readiness['host']}:{readiness['port']}")
    try:
        with pytest.raises(grpc.aio.AioRpcError) as error:
            await engine_pb2_grpc.EngineWorkerStub(channel).Describe(engine_pb2.DescribeRequest())

        assert error.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        await channel.close()
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


class TestServicer(engine_pb2_grpc.EngineWorkerServicer):
    async def Describe(
        self, request: engine_pb2.DescribeRequest, context: grpc.aio.ServicerContext
    ) -> engine_pb2.DescribeResponse:
        return engine_pb2.DescribeResponse()
