import asyncio
import sys
import threading
from pathlib import Path

import grpc
import pytest
from tts_studio_protocol.engine.v1 import engine_pb2, engine_pb2_grpc

sys.path.insert(0, str(Path(__file__).parents[2] / "workers" / "openai_compatible" / "src"))

from tts_studio_openai_worker import http as provider_http
from tts_studio_openai_worker import service as worker_service
from tts_studio_openai_worker.http import ProviderSynthesis


class _Context:
    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return (("x-tts-worker-token", "worker-token"),)

    def cancelled(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_provider_alignment_requires_authentication_and_reports_unavailable() -> None:
    server = grpc.aio.server()
    engine_pb2_grpc.add_EngineWorkerServicer_to_server(
        worker_service.OpenAICompatibleWorker("worker-token"),
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            client = engine_pb2_grpc.EngineWorkerStub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as rejected:
                await client.Align(engine_pb2.AlignRequest(), timeout=2)
            assert rejected.value.code() == grpc.StatusCode.UNAUTHENTICATED
            response = await client.Align(
                engine_pb2.AlignRequest(),
                metadata=(("x-tts-worker-token", "worker-token"),),
                timeout=2,
            )
            assert response.error.code == "alignment_unavailable"
            assert response.error.retryable is False
    finally:
        await server.stop(0)


class _CancellableContext(_Context):
    def __init__(self) -> None:
        self._cancelled = False
        self._callbacks: list[object] = []

    def cancelled(self) -> bool:
        return self._cancelled

    def add_done_callback(self, callback: object) -> None:
        self._callbacks.append(callback)

    def cancel(self) -> None:
        self._cancelled = True
        for callback in self._callbacks:
            callback(self)


@pytest.mark.asyncio
async def test_worker_streams_provider_pcm_as_authenticated_protocol_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synthesis_requests: list[dict[str, object]] = []

    async def fake_synthesize(**kwargs: object) -> ProviderSynthesis:
        synthesis_requests.append(kwargs)
        return ProviderSynthesis(sample_rate_hz=48000, frames=3, pcm=b"\x01\x00" * 3)

    monkeypatch.setattr(worker_service, "synthesize", fake_synthesize)
    worker = worker_service.OpenAICompatibleWorker("worker-token")
    request = engine_pb2.SynthesizeRequest(model_id="provider:one", text="hello", voice_id="alloy")
    request.provider.base_url = "https://provider.example/v1"
    request.provider.model = "tts-1"
    request.provider.api_key = "secret"
    request.options.speed = 1.25

    events = [event async for event in worker.Synthesize(request, _Context())]

    assert len(synthesis_requests) == 1
    synthesis_request = dict(synthesis_requests[0])
    assert isinstance(synthesis_request.pop("cancellation"), provider_http.ProviderCancellation)
    assert synthesis_request == {
        "base_url": "https://provider.example/v1",
        "api_key": "secret",
        "model": "tts-1",
        "text": "hello",
        "voice": "alloy",
        "speed": 1.25,
    }
    assert events[0].HasField("header")
    assert events[0].header.sample_rate_hz == 48000
    assert events[1].chunk.pcm == b"\x01\x00" * 3
    assert events[-1].result.total_frames == 3


@pytest.mark.asyncio
async def test_blocked_dns_cancellation_terminates_rpc_and_quarantines_before_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolution_started = threading.Event()
    release_resolution = threading.Event()
    credentialed_operation = threading.Event()

    def blocked_resolution(host: str, port: int, *args: object, **kwargs: object):
        del host, args, kwargs
        resolution_started.set()
        release_resolution.wait()
        return [
            (
                provider_http.socket.AF_INET,
                provider_http.socket.SOCK_STREAM,
                6,
                "",
                ("93.184.216.34", port),
            )
        ]

    class ForbiddenOpener:
        def open(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            credentialed_operation.set()
            raise OSError("credentialed request must not continue after cancellation")

    monkeypatch.setattr(provider_http.socket, "getaddrinfo", blocked_resolution)
    monkeypatch.setattr(
        provider_http.urllib.request, "build_opener", lambda *handlers: ForbiddenOpener()
    )
    worker = worker_service.OpenAICompatibleWorker("worker-token")
    context = _CancellableContext()
    request = engine_pb2.SynthesizeRequest(model_id="provider:one", text="hello", voice_id="alloy")
    request.provider.base_url = "https://provider.example/v1"
    request.provider.model = "tts-1"
    request.provider.api_key = "secret"
    stream = worker.Synthesize(request, context)
    first_event = asyncio.create_task(anext(stream))

    assert await asyncio.to_thread(resolution_started.wait, 1.0)
    context.cancel()

    async def release_later() -> None:
        await asyncio.sleep(1.0)
        release_resolution.set()

    release_task = asyncio.create_task(release_later())
    try:
        event = await asyncio.wait_for(first_event, timeout=2.0)
        assert event.error.code == "synthesis_cancelled"
        assert not release_task.done()
        assert worker._provider_quarantined is True
        health = await worker.Health(None, _Context())
        assert health.status == engine_pb2.HealthResponse.DEGRADED
        await release_task
        await asyncio.sleep(0.05)
        assert not credentialed_operation.is_set()
    finally:
        release_resolution.set()
        await asyncio.gather(release_task, return_exceptions=True)
        if not first_event.done():
            first_event.cancel()
            await asyncio.gather(first_event, return_exceptions=True)


@pytest.mark.asyncio
async def test_worker_rejects_missing_provider_configuration_without_secret_details() -> None:
    worker = worker_service.OpenAICompatibleWorker("worker-token")
    request = engine_pb2.SynthesizeRequest(model_id="provider:one", text="hello", voice_id="alloy")

    events = [event async for event in worker.Synthesize(request, _Context())]

    assert events[0].error.code == "provider_configuration_missing"
    assert "worker-token" not in events[0].error.message
