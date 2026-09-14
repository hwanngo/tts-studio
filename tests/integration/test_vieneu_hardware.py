from __future__ import annotations

import os
import runpy
import subprocess
from pathlib import Path

import pytest

_CACHE_VALIDATOR = runpy.run_path(
    Path(__file__).resolve().parents[2] / "scripts" / "check_vieneu_hardware_cache.py"
)
cache_prerequisite_reason = _CACHE_VALIDATOR["cache_prerequisite_reason"]

_RUN = os.environ.get("TTS_STUDIO_RUN_VIENEU_HARDWARE") == "1"
_MARKER = os.environ.get("TTS_STUDIO_VIENEU_MODEL_CACHE")
_MODEL_CACHE = Path(_MARKER).expanduser() if _MARKER else None


def _cache_prerequisite_reason() -> str | None:
    if not _RUN:
        return "set TTS_STUDIO_RUN_VIENEU_HARDWARE=1 to enable the VieNeu hardware gate"
    return cache_prerequisite_reason(_MODEL_CACHE)


_PREREQUISITE_REASON = _cache_prerequisite_reason()

pytestmark = pytest.mark.skipif(
    _PREREQUISITE_REASON is not None,
    reason=_PREREQUISITE_REASON or "VieNeu hardware prerequisites are unavailable",
)


def test_vieneu_real_model_gate_prerequisites_are_explicit() -> None:
    assert _MODEL_CACHE is not None
    assert cache_prerequisite_reason(_MODEL_CACHE) is None


def test_vieneu_real_model_variants_reload_offline_and_stream() -> None:
    assert _MODEL_CACHE is not None
    script = r"""
import asyncio, json, sys
from pathlib import Path
import wave
import grpc
from tts_studio_protocol.engine.v1 import engine_pb2, engine_pb2_grpc
from tts_studio_vieneu_worker.service import VieNeuEngineWorker

async def main():
    token = "hardware-gate-token"
    server = grpc.aio.server()
    engine_pb2_grpc.add_EngineWorkerServicer_to_server(
        VieNeuEngineWorker(token, Path(sys.argv[1])), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    stub = engine_pb2_grpc.EngineWorkerStub(channel)
    metadata = (("x-tts-worker-token", token),)
    reference_path = Path(sys.argv[1]) / "staging" / "references" / "hardware-reference.wav"
    try:
        reference_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(reference_path), "wb") as reference:
            reference.setnchannels(1)
            reference.setsampwidth(2)
            reference.setframerate(16000)
            reference.writeframes(b"\x00\x00" * 16000)
        voices = None
        for variant in ("int8", "fp32"):
            loaded = await stub.LoadModel(
                engine_pb2.LoadModelRequest(
                    model_id="hardware-model", cache_path="models/vieneu", variant=variant
                ), metadata=metadata, timeout=120
            )
            assert loaded.loaded, loaded
            listed = await stub.ListVoices(
                engine_pb2.ListVoicesRequest(model_id="hardware-model"), metadata=metadata
            )
            assert listed.voices, listed
            voices = listed.voices
            stream = stub.Synthesize(
                engine_pb2.SynthesizeRequest(
                    model_id="hardware-model", voice_id=voices[0].id, text="hardware gate"
                ), metadata=metadata, timeout=300
            )
            events = [event async for event in stream]
            assert events[0].header.sample_rate_hz == 48000
            assert events[-1].result.total_frames > 0

            supported = {item.name for item in (await stub.Describe(
                engine_pb2.DescribeRequest(), metadata=metadata
            )).capabilities if item.supported}
            if "reference_cloning" in supported:
                validation = await stub.ValidateReference(
                    engine_pb2.ValidateReferenceRequest(
                        model_id="hardware-model",
                        reference_path="staging/references/hardware-reference.wav",
                        transcript="hardware reference",
                    ),
                    metadata=metadata,
                )
                assert validation.valid
                assert validation.metadata.duration_ms == 1000
                reference_stream = stub.Synthesize(
                    engine_pb2.SynthesizeRequest(
                        model_id="hardware-model",
                        reference=engine_pb2.ReferenceAudio(
                            reference_path="staging/references/hardware-reference.wav",
                            transcript="hardware reference",
                        ),
                        text="hardware reference generation",
                    ),
                    metadata=metadata,
                    timeout=300,
                )
                reference_events = [event async for event in reference_stream]
                assert reference_events[0].header.sample_rate_hz == 48000
                assert reference_events[-1].result.total_frames > 0

            cancelled_stream = stub.Synthesize(
                engine_pb2.SynthesizeRequest(
                    model_id="hardware-model",
                    voice_id=voices[0].id,
                    text="hardware gate " * 256,
                ),
                metadata=metadata,
                timeout=30,
            )
            cancellation_iterator = cancelled_stream.__aiter__()
            first_cancel_event = await anext(cancellation_iterator)
            assert first_cancel_event.HasField("header"), first_cancel_event
            cancelled_stream.cancel()
            cancellation_events = []
            cancellation_rpc_error = None
            locally_cancelled = False
            try:
                async for event in cancellation_iterator:
                    cancellation_events.append(event)
            except asyncio.CancelledError:
                assert cancelled_stream.cancelled()
                locally_cancelled = True
            except grpc.aio.AioRpcError as error:
                cancellation_rpc_error = error
            cancellation_errors = [
                event.error
                for event in cancellation_events
                if event.HasField("error")
            ]
            if cancellation_errors:
                assert cancellation_errors[-1].code == "synthesis_cancelled"
                assert cancellation_errors[-1].details["terminal_status"] == "CANCELLED"
            elif not locally_cancelled:
                assert cancellation_rpc_error is not None
                assert cancellation_rpc_error.code() == grpc.StatusCode.CANCELLED
            await stub.UnloadModel(
                engine_pb2.UnloadModelRequest(model_id="hardware-model"), metadata=metadata
            )
            reloaded = await stub.LoadModel(
                engine_pb2.LoadModelRequest(
                    model_id="hardware-model", cache_path="models/vieneu", variant=variant
                ), metadata=metadata, timeout=120
            )
            assert reloaded.loaded
            reloaded_stream = stub.Synthesize(
                engine_pb2.SynthesizeRequest(
                    model_id="hardware-model", voice_id=voices[0].id, text="reloaded hardware gate"
                ),
                metadata=metadata,
                timeout=300,
            )
            reloaded_events = [event async for event in reloaded_stream]
            assert reloaded_events[0].header.sample_rate_hz == 48000
            assert reloaded_events[-1].result.total_frames > 0
            await stub.UnloadModel(
                engine_pb2.UnloadModelRequest(model_id="hardware-model"), metadata=metadata
            )
    finally:
        reference_path.unlink(missing_ok=True)
        await channel.close()
        await server.stop(0)

asyncio.run(main())
"""
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("MYPYPATH", None)
    subprocess.run(
        [
            "uv",
            "run",
            "--offline",
            "--frozen",
            "--project",
            "workers/vieneu",
            "python",
            "-c",
            script,
            str(_MODEL_CACHE),
        ],
        check=True,
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
    )
