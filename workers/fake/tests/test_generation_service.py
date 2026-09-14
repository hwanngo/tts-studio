import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

import grpc
import pytest
from tts_studio_fake_worker.service import FakeEngineWorker
from tts_studio_protocol.engine.v1 import engine_pb2, engine_pb2_grpc
from tts_studio_worker_sdk.server import serve_worker

TOKEN = "fake-worker-test-token"
MODEL = "fixtures/compatible"


@pytest.fixture
def worker_servicer() -> FakeEngineWorker:
    return FakeEngineWorker(TOKEN)


@pytest.fixture
async def worker(
    tmp_path: Path, worker_servicer: FakeEngineWorker
) -> AsyncIterator[engine_pb2_grpc.EngineWorkerStub]:
    ready_file = tmp_path / "run" / "worker-ready.json"
    task = asyncio.create_task(
        serve_worker(
            worker_servicer,
            host="127.0.0.1",
            port=0,
            token=TOKEN,
            ready_file=ready_file,
        )
    )
    try:
        async with asyncio.timeout(2):
            while not ready_file.exists():
                await asyncio.sleep(0.01)
        readiness = json.loads(ready_file.read_text(encoding="utf-8"))
        channel = grpc.aio.insecure_channel(f"{readiness['host']}:{readiness['port']}")
        try:
            yield engine_pb2_grpc.EngineWorkerStub(channel)
        finally:
            await channel.close()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def _load(stub: engine_pb2_grpc.EngineWorkerStub) -> None:
    response = await stub.LoadModel(
        engine_pb2.LoadModelRequest(model_id=MODEL, cache_path="models/compatible", variant="fp32"),
        metadata=(("x-tts-worker-token", TOKEN),),
    )
    assert response.loaded is True
    assert not response.HasField("error")


@pytest.mark.asyncio
async def test_authenticated_lifecycle_is_idempotent_and_voices_are_runtime_facts(
    worker: engine_pb2_grpc.EngineWorkerStub,
) -> None:
    await _load(worker)

    voices = await worker.ListVoices(
        engine_pb2.ListVoicesRequest(model_id=MODEL),
        metadata=(("x-tts-worker-token", TOKEN),),
    )
    assert [(voice.id, voice.label, tuple(voice.capabilities)) for voice in voices.voices] == [
        ("fake-neutral", "Fake Neutral", ("preset", "deterministic"))
    ]

    first = await worker.UnloadModel(
        engine_pb2.UnloadModelRequest(model_id=MODEL),
        metadata=(("x-tts-worker-token", TOKEN),),
    )
    second = await worker.UnloadModel(
        engine_pb2.UnloadModelRequest(model_id=MODEL),
        metadata=(("x-tts-worker-token", TOKEN),),
    )
    assert first.unloaded is True
    assert second.unloaded is True


@pytest.mark.asyncio
async def test_synthesis_is_deterministic_and_orders_header_chunks_progress_result(
    worker: engine_pb2_grpc.EngineWorkerStub,
) -> None:
    await _load(worker)
    request = engine_pb2.SynthesizeRequest(
        model_id=MODEL, voice_id="fake-neutral", text="hello deterministic worker"
    )

    async def collect() -> list[engine_pb2.SynthesisEvent]:
        return [
            event
            async for event in worker.Synthesize(request, metadata=(("x-tts-worker-token", TOKEN),))
        ]

    first = await collect()
    second = await collect()
    payloads = [event.WhichOneof("payload") for event in first]
    assert payloads[0] == "header"
    assert set(payloads[1:-2]) == {"chunk"}
    assert payloads[-2:] == ["progress", "result"]
    assert first == second
    assert first[0].header == engine_pb2.AudioHeader(
        sample_rate_hz=48000,
        channels=1,
        sample_format=engine_pb2.S16LE,
    )
    chunks = [event.chunk for event in first if event.HasField("chunk")]
    assert [chunk.sequence for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.pcm for chunk in chunks)
    assert first[-2].progress.duration_frames == first[-1].result.total_frames
    assert first[-1].result.duration_ms == round(first[-1].result.total_frames / 48000 * 1000)
    assert (
        hashlib.sha256(b"".join(chunk.pcm for chunk in chunks)).hexdigest()
        == hashlib.sha256(
            b"".join(event.chunk.pcm for event in second if event.HasField("chunk"))
        ).hexdigest()
    )


@pytest.mark.asyncio
async def test_synthesis_reports_structured_errors_for_invalid_requests(
    worker: engine_pb2_grpc.EngineWorkerStub,
) -> None:
    unloaded = await worker.ListVoices(
        engine_pb2.ListVoicesRequest(model_id=MODEL),
        metadata=(("x-tts-worker-token", TOKEN),),
    )
    assert unloaded.error.code == "model_not_loaded"
    assert unloaded.error.retryable is False

    await _load(worker)
    unknown_voice = [
        event
        async for event in worker.Synthesize(
            engine_pb2.SynthesizeRequest(model_id=MODEL, voice_id="missing", text="hello"),
            metadata=(("x-tts-worker-token", TOKEN),),
        )
    ]
    assert unknown_voice[0].error.code == "voice_not_found"
    assert unknown_voice[0].error.retryable is False

    malformed = [
        event
        async for event in worker.Synthesize(
            engine_pb2.SynthesizeRequest(model_id=MODEL, voice_id="fake-neutral", text=""),
            metadata=(("x-tts-worker-token", TOKEN),),
        )
    ]
    assert malformed[0].error.code == "invalid_request"

    incompatible = await worker.LoadModel(
        engine_pb2.LoadModelRequest(model_id="fixtures/incompatible", variant="fp32"),
        metadata=(("x-tts-worker-token", TOKEN),),
    )
    assert incompatible.loaded is False
    assert incompatible.error.code == "model_incompatible"


@pytest.mark.asyncio
async def test_load_model_accepts_opaque_core_installation_id(
    worker: engine_pb2_grpc.EngineWorkerStub,
) -> None:
    response = await worker.LoadModel(
        engine_pb2.LoadModelRequest(
            model_id="c3f38314-aa84-4531-8e1f-d5a96cdfa5c9",
            cache_path="models/c3f38314-aa84-4531-8e1f-d5a96cdfa5c9",
            variant="fp32",
        ),
        metadata=(("x-tts-worker-token", TOKEN),),
    )

    assert response.loaded is True
    assert not response.HasField("error")


@pytest.mark.asyncio
async def test_synthesis_honors_cancellation_and_deadline(
    worker: engine_pb2_grpc.EngineWorkerStub,
    worker_servicer: FakeEngineWorker,
) -> None:
    await _load(worker)
    request = engine_pb2.SynthesizeRequest(
        model_id=MODEL, voice_id="fake-neutral", text="long " * 4000
    )

    call = worker.Synthesize(request, metadata=(("x-tts-worker-token", TOKEN),))
    first = await call.read()
    assert first.WhichOneof("payload") == "header"
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call.read()
    async with asyncio.timeout(2):
        while not worker_servicer.termination_errors:
            await asyncio.sleep(0.001)
    cancellation_errors = [
        error for error in worker_servicer.termination_errors if error.code == "synthesis_cancelled"
    ]
    assert cancellation_errors
    assert cancellation_errors[-1].retryable is False
    assert cancellation_errors[-1].details["terminal_status"] == "CANCELLED"

    with pytest.raises(grpc.aio.AioRpcError) as deadline:
        observed: list[engine_pb2.SynthesisEvent] = []
        events = worker.Synthesize(
            request,
            timeout=0.5,
            metadata=(("x-tts-worker-token", TOKEN),),
        )
        async for _ in events:
            observed.append(_)
    assert deadline.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
    structured_errors = [event.error for event in observed if event.HasField("error")]
    assert structured_errors, [event.WhichOneof("payload") for event in observed]
    assert structured_errors[-1].code == "synthesis_deadline_exceeded"
    assert structured_errors[-1].retryable is True
    assert structured_errors[-1].details["terminal_status"] == "DEADLINE_EXCEEDED"
