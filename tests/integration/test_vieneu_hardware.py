from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

_RUN = os.environ.get("TTS_STUDIO_RUN_VIENEU_HARDWARE") == "1"
_MARKER = os.environ.get("TTS_STUDIO_VIENEU_MODEL_CACHE")
_MODEL_CACHE = Path(_MARKER).expanduser() if _MARKER else None
_MODEL_COMMIT = "8b7e9cffb4b41918cb638b9f62f0a751184d14a6"
_CODEC_REVISION = "ceff0d0749bfb3fa2d61149794ec6feef0d1e1ae"
_SDK_VERSION = "3.6.3"
_GRAPH_FILES = (
    "config.json",
    "tokenizer.json",
    "vieneu_acoustic_cached.onnx",
    "vieneu_decode_step.onnx",
    "vieneu_prefill.onnx",
    "vieneu_backbone_shared.data",
    "vieneu_v3_heads.npz",
)
_CODEC_FILES = (
    "codec_browser_onnx_meta.json",
    "moss_audio_tokenizer_decode_full.onnx",
    "moss_audio_tokenizer_decode_shared.data",
    "moss_audio_tokenizer_decode_step.onnx",
    "moss_audio_tokenizer_encode.data",
    "moss_audio_tokenizer_encode.onnx",
)


def _is_safe_regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _cache_prerequisite_reason() -> str | None:
    if not _RUN:
        return "set TTS_STUDIO_RUN_VIENEU_HARDWARE=1 to enable the VieNeu hardware gate"
    if _MODEL_CACHE is None:
        return "set TTS_STUDIO_VIENEU_MODEL_CACHE to an exact local VieNeu cache"
    if _MODEL_CACHE.is_symlink() or not _MODEL_CACHE.is_dir():
        return f"VieNeu cache directory does not exist: {_MODEL_CACHE}"
    marker = _MODEL_CACHE / ".vieneu-model-cache-marker"
    if marker.is_symlink() or not marker.is_file():
        return "VieNeu cache marker is missing or is not a regular file"
    try:
        provenance = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "VieNeu cache marker is not valid JSON"
    if not isinstance(provenance, dict):
        return "VieNeu cache marker must contain a JSON object"
    if provenance.get("model_commit") != _MODEL_COMMIT:
        return "VieNeu cache marker does not contain the required pinned model revision"
    if provenance.get("codec_revision") != _CODEC_REVISION:
        return "VieNeu cache marker does not contain the required pinned codec revision"
    if provenance.get("sdk_version") != _SDK_VERSION:
        return "VieNeu cache marker does not contain the required VieNeu SDK version"
    for variant in ("int8", "fp32"):
        variant_root = _MODEL_CACHE / "models" / "vieneu" / "backbone" / variant
        if not variant_root.is_dir() or variant_root.is_symlink():
            return f"VieNeu cache is missing the {variant} model directory"
        if not all(_is_safe_regular_file(variant_root / name) for name in _GRAPH_FILES):
            return f"VieNeu cache is missing safe {variant} model files"
    codec_root = _MODEL_CACHE / "models" / "vieneu" / "codec"
    if not codec_root.is_dir() or codec_root.is_symlink():
        return "VieNeu cache is missing the codec directory"
    if not all(_is_safe_regular_file(codec_root / name) for name in _CODEC_FILES):
        return "VieNeu cache is missing safe codec files"
    return None


_PREREQUISITE_REASON = _cache_prerequisite_reason()

pytestmark = pytest.mark.skipif(
    _PREREQUISITE_REASON is not None,
    reason=_PREREQUISITE_REASON or "VieNeu hardware prerequisites are unavailable",
)


def test_vieneu_real_model_gate_prerequisites_are_explicit() -> None:
    assert _MODEL_CACHE is not None
    marker = _MODEL_CACHE / ".vieneu-model-cache-marker"
    assert marker.is_file()
    provenance = json.loads(marker.read_text(encoding="utf-8"))
    assert isinstance(provenance, dict)
    assert provenance.get("model_commit") == _MODEL_COMMIT
    assert provenance.get("codec_revision") == _CODEC_REVISION
    assert provenance.get("sdk_version") == _SDK_VERSION


def test_vieneu_real_model_variants_reload_offline_and_stream() -> None:
    assert _MODEL_CACHE is not None
    script = r'''
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
            first_cancel_event = await anext(cancelled_stream)
            assert first_cancel_event.HasField("header"), first_cancel_event
            cancelled_stream.cancel()
            cancellation_events = []
            cancellation_rpc_error = None
            try:
                async for event in cancelled_stream:
                    cancellation_events.append(event)
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
            else:
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
'''
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
