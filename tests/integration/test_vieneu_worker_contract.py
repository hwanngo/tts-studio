from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import grpc
import pytest
from httpx import ASGITransport, AsyncClient
from tts_studio_protocol.engine.v1 import engine_pb2, engine_pb2_grpc

from tts_studio.config import Settings
from tts_studio.events import UnsafeEventPayloadError
from tts_studio.models.registry import DownloadState, ModelRegistry
from tts_studio.server.app import create_app
from tts_studio.storage.db import Database
from tts_studio.storage.layout import StorageLayout
from tts_studio.workers.adapters import AdapterDescriptor
from tts_studio.workers.process import WorkerLaunchSpec
from tts_studio.workers.supervisor import WorkerSupervisor

TOKEN = "contract-token"
MODEL_ID = "vieneu-contract-model"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CAPABILITIES = (
    "health",
    "model_validation",
    "model_download",
    "model_lifecycle",
    "preset_voices",
    "streaming_synthesis",
    "synthesis_cancellation",
    "reference_cloning",
)
GRAPH_FILES = (
    "config.json",
    "tokenizer.json",
    "vieneu_acoustic_cached.onnx",
    "vieneu_decode_step.onnx",
    "vieneu_prefill.onnx",
    "vieneu_backbone_shared.data",
    "vieneu_v3_heads.npz",
)
CODEC_FILES = (
    "codec_browser_onnx_meta.json",
    "moss_audio_tokenizer_decode_full.onnx",
    "moss_audio_tokenizer_decode_shared.data",
    "moss_audio_tokenizer_decode_step.onnx",
    "moss_audio_tokenizer_encode.data",
    "moss_audio_tokenizer_encode.onnx",
)
CLONING_FILES = ("denoiser.onnx", "speaker_encoder.onnx")
MODEL_COMMIT = "b" * 40
CODEC_COMMIT = "ceff0d0749bfb3fa2d61149794ec6feef0d1e1ae"


class FakeSdk:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.released = False

    def list_preset_voices(self) -> list[tuple[str, str]]:
        return [("Contract Voice", "contract-voice")]

    def infer_stream(
        self,
        text: str,
        *,
        voice: str | None = None,
        ref_audio: str | None = None,
        ref_text: str | None = None,
    ):
        if ref_audio is not None:
            assert voice is None
            assert ref_text == "reference transcript"
            assert Path(ref_audio).is_file()
        else:
            assert text == "contract text"
            assert voice == "contract-voice"
        import numpy as np

        if text.startswith("cancel"):
            for _ in range(200):
                time.sleep(0.005)
                yield np.array([0.0, 0.5, -0.5, 1.2], dtype=np.float32)
        else:
            yield np.array([0.0, 0.5, -0.5, 1.2], dtype=np.float32)

    def release(self) -> None:
        self.released = True


def _factory(**kwargs: Any) -> FakeSdk:
    return FakeSdk(**kwargs)


def _make_cache(root: Path) -> None:
    for variant in ("int8", "fp32"):
        directory = root / "models" / "contract" / "backbone" / variant
        directory.mkdir(parents=True)
        for name in GRAPH_FILES:
            (directory / name).write_bytes(b"fixture")
    codec = root / "models" / "contract" / "codec"
    codec.mkdir(parents=True)
    for name in CODEC_FILES:
        (codec / name).write_bytes(b"fixture")
    cloning = root / "models" / "contract" / "cloning"
    cloning.mkdir(parents=True)
    for name in CLONING_FILES:
        (cloning / name).write_bytes(b"fixture")


def _activate_contract_model(root: Path) -> None:
    layout = StorageLayout.from_root(root)
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    registry = ModelRegistry(database)
    registry.upsert_engine_installation(
        engine_installation_id="vieneu@3.6.3",
        engine_id="vieneu",
        version="3.6.3",
        command=[
            "uv",
            "run",
            "--frozen",
            "--project",
            "workers/vieneu",
            "tts-studio-vieneu-worker",
        ],
        working_directory=str(_REPOSITORY_ROOT),
        environment={},
        capabilities={"model_lifecycle": True, "preset_voices": True, "streaming_synthesis": True},
        lifecycle_state="ready",
    )
    download = registry.create_download_job(
        job_id="contract-download",
        repository_id="pnnbao-ump/VieNeu-TTS-v3-Turbo",
        requested_revision=MODEL_COMMIT,
        engine_installation_id="vieneu@3.6.3",
        staging_path="staging/contract-download",
        correlation_id="contract-download-correlation",
    )
    for state in (
        DownloadState.VALIDATING,
        DownloadState.DOWNLOADING,
        DownloadState.VERIFYING,
        DownloadState.ACTIVATING,
    ):
        registry.transition_download_job(download.id, state)
    registry.activate_model(
        download_job_id=download.id,
        model_id=MODEL_ID,
        repository_id="pnnbao-ump/VieNeu-TTS-v3-Turbo",
        requested_revision=MODEL_COMMIT,
        resolved_commit=MODEL_COMMIT,
        engine_installation_id="vieneu@3.6.3",
        compatibility_evidence={"engine_id": "vieneu"},
        runtime_variant="fp32",
        manifest={},
        checksum_summary={},
        byte_size=1,
        cache_path="models/contract",
        desired_load_state="unloaded",
        observed_load_state="unloaded",
        replica_summary={"active_generations": 0},
    )


@asynccontextmanager
async def _worker_server(data_root: Path):
    script = """
import asyncio, sys
import time
from pathlib import Path
import numpy as np
import grpc
from tts_studio_protocol.engine.v1 import engine_pb2_grpc
from tts_studio_vieneu_worker.service import VieNeuEngineWorker

class FakeSdk:
    def list_preset_voices(self): return [("Contract Voice", "contract-voice")]
    def infer_stream(self, text, *, voice=None, ref_audio=None, ref_text=None):
        if ref_audio is not None:
            assert voice is None
            assert ref_text == "reference transcript"
        if text.startswith("cancel"):
            for _ in range(200):
                time.sleep(0.005)
                yield np.array([0.0, 0.5, -0.5, 1.2], dtype=np.float32)
        else:
            yield np.array([0.0, 0.5, -0.5, 1.2], dtype=np.float32)
    def release(self): pass

def factory(**kwargs): return FakeSdk()
factory.infer_stream = FakeSdk.infer_stream

async def main():
    server = grpc.aio.server()
    engine_pb2_grpc.add_EngineWorkerServicer_to_server(
        VieNeuEngineWorker(sys.argv[1], Path(sys.argv[2]), vieneu_factory=factory), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    print(port, flush=True)
    await server.wait_for_termination()

asyncio.run(main())
"""
    process = subprocess.Popen(  # noqa: ASYNC220 - fixture process must outlive this context
        ["uv", "run", "--project", "workers/vieneu", "python", "-c", script, TOKEN, str(data_root)],
        cwd=_REPOSITORY_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "MYPYPATH"}
        },
    )
    assert process.stdout is not None
    line = await asyncio.to_thread(process.stdout.readline)
    if not line:
        stderr = process.stderr.read() if process.stderr is not None else ""
        raise AssertionError(f"VieNeu fixture Worker failed to start: {stderr}")
    port = int(line)
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield channel
    finally:
        await channel.close()
        process.terminate()
        await asyncio.to_thread(process.wait, 10)


def _core_worker_script() -> str:
    return r"""
import argparse, asyncio, os
from pathlib import Path
from types import SimpleNamespace
from tts_studio_protocol.engine.v1 import engine_pb2
from tts_studio_worker_sdk.auth import consume_worker_token
from tts_studio_worker_sdk.server import serve_worker
from tts_studio_vieneu_worker.constants import MOSS_CODEC_COMMIT, MOSS_CODEC_REPOSITORY, VIENEU_REPOSITORY
from tts_studio_vieneu_worker.service import VieNeuEngineWorker

MODEL_COMMIT = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
GRAPH = ("config.json", "tokenizer.json", "vieneu_acoustic_cached.onnx", "vieneu_decode_step.onnx", "vieneu_prefill.onnx", "vieneu_backbone_shared.data", "vieneu_v3_heads.npz")
CODEC = ("codec_browser_onnx_meta.json", "moss_audio_tokenizer_decode_full.onnx", "moss_audio_tokenizer_decode_shared.data", "moss_audio_tokenizer_decode_step.onnx", "moss_audio_tokenizer_encode.data", "moss_audio_tokenizer_encode.onnx")

class Repository:
    def model_info(self, repo_id, revision=None):
        return SimpleNamespace(sha=MOSS_CODEC_COMMIT if repo_id == MOSS_CODEC_REPOSITORY else MODEL_COMMIT)
    def list_files(self, repo_id, revision):
        names = [f"{variant}/{name}" for variant in ("onnx_int8", "onnx_update") for name in GRAPH]
        names += ["denoiser.onnx", "speaker_encoder.onnx"] if repo_id == VIENEU_REPOSITORY else list(CODEC)
        return [SimpleNamespace(rfilename=name, size=7) for name in names]
    def snapshot_download(self, repo_id, revision, destination, allow_patterns):
        destination.mkdir(parents=True, exist_ok=True)
        for name in allow_patterns:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((repo_id + ":" + name).encode())

class Sdk:
    def __init__(self, **kwargs):
        if os.environ.get("TTS_STUDIO_CONTRACT_SECRET_SENTINEL") is not None:
            raise RuntimeError("contract configuration sentinel was forwarded to Worker")
        self.kwargs = kwargs
    def list_preset_voices(self):
        marker = Path(self.kwargs["onnx_dir"]).parents[3] / "run" / "privacy-worker-error.txt"
        if marker.is_file():
            raise RuntimeError(marker.read_text(encoding="utf-8"))
        return [("Core Contract Voice", "core-contract-voice")]
    def infer_stream(self, text, *, voice=None, ref_audio=None, ref_text=None):
        import numpy as np, time
        if text.startswith("cancel"):
            for _ in range(300):
                time.sleep(0.003)
                yield np.array([0.0, 0.5, -0.5, 1.2], dtype=np.float32)
        else:
            yield np.array([0.0, 0.5, -0.5, 1.2], dtype=np.float32)
    def release(self): pass

def factory(**kwargs): return Sdk(**kwargs)

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--ready-file", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--owner-claim")
    args = parser.parse_args()
    token = consume_worker_token(args.token_file)
    await serve_worker(VieNeuEngineWorker(token, args.data_dir, repository=Repository(), vieneu_factory=factory), args.host, args.port, token, args.ready_file)

asyncio.run(main())
"""


async def _wait_for_generation_state(service: Any, job_id: str, expected: str) -> None:
    async def wait() -> None:
        while True:
            state = service.get(job_id).state.value
            if state == expected:
                return
            if state in {"completed", "cancelled", "failed"}:
                raise AssertionError(
                    f"generation reached terminal state {state!r} before {expected!r}"
                )
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout=5)


@pytest.mark.asyncio
async def test_public_vieneu_voice_listing_then_generation_uses_real_worker_lifecycle(
    tmp_path: Path,
) -> None:
    layout_root = tmp_path / "data"
    _make_cache(layout_root)
    _activate_contract_model(layout_root)
    supervisor = WorkerSupervisor(StorageLayout.from_root(layout_root), startup_timeout=10)
    launch = WorkerLaunchSpec(
        command=(
            "uv",
            "run",
            "--frozen",
            "--project",
            "workers/vieneu",
            "python",
            "-c",
            _core_worker_script(),
        ),
        cwd=_REPOSITORY_ROOT,
    )
    await supervisor.start("vieneu", launch)
    adapter = AdapterDescriptor(engine_id="vieneu", priority=1, launch=launch)
    app = create_app(Settings.resolve(layout_root), supervisor=supervisor, adapters=(adapter,))
    try:
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            voices = await client.get("/api/v1/voices", params={"model_id": MODEL_ID})
            assert voices.status_code == 200
            assert voices.json() == [
                {
                    "id": "core-contract-voice",
                    "label": "Core Contract Voice",
                    "capabilities": ["preset"],
                }
            ]
            assert "reference_cloning" in supervisor._workers["vieneu"].capabilities.supported

            generated = await client.post(
                "/api/v1/generations",
                json={
                    "model_id": MODEL_ID,
                    "voice_id": "core-contract-voice",
                    "text": "contract text",
                },
            )
            assert generated.status_code == 202, generated.text
            completed = await app.state.generation_service.wait(generated.json()["id"])
            assert completed.state.value == "completed", completed.error
    finally:
        await supervisor.stop_all()


@pytest.mark.asyncio
async def test_core_acquisition_activation_voices_generation_and_privacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout_root = tmp_path / "data"
    secret = "hf_" + "contractsentinel" * 3
    url = "https://private.example.invalid/vieneu/contract"
    absolute_path = str(layout_root / "private" / "model.bin")
    traceback_text = (
        "Traceback (most recent call last):\n"
        f'  File "{absolute_path}", line 7, in load\n'
        "RuntimeError: contract adapter failure\n"
    )
    privacy_diagnostics = f"{secret}\n{url}\n{absolute_path}\n{traceback_text}"
    monkeypatch.setenv("TTS_STUDIO_CONTRACT_SECRET_SENTINEL", secret)
    supervisor = WorkerSupervisor(StorageLayout.from_root(layout_root), startup_timeout=10)
    launch = WorkerLaunchSpec(
        command=(
            "uv",
            "run",
            "--frozen",
            "--project",
            "workers/vieneu",
            "python",
            "-c",
            _core_worker_script(),
        ),
        cwd=_REPOSITORY_ROOT,
    )
    adapter = AdapterDescriptor(engine_id="vieneu", priority=1, launch=launch)
    await supervisor.start("vieneu", launch)
    app = create_app(Settings.resolve(layout_root), supervisor=supervisor, adapters=(adapter,))
    try:
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
        ):
            validation = await client.post(
                "/api/v1/models/validate",
                json={
                    "repository_id": "pnnbao-ump/VieNeu-TTS-v3-Turbo",
                    "requested_revision": MODEL_COMMIT,
                },
            )
            assert validation.status_code == 200
            assert validation.json()["compatible"] is True
            assert validation.json()["selected_engine_id"] == "vieneu"

            queued = await client.post(
                "/api/v1/downloads",
                json={
                    "repository_id": "pnnbao-ump/VieNeu-TTS-v3-Turbo",
                    "requested_revision": MODEL_COMMIT,
                    "variant": "fp32",
                },
            )
            assert queued.status_code == 202
            completed = await app.state.model_service.wait_for_download(queued.json()["id"])
            assert completed.state.value == "completed", completed.error
            model = (await client.get("/api/v1/models")).json()[0]
            assert model["resolved_commit"] == MODEL_COMMIT
            assert model["cache_path"].startswith("models/")
            model_id = model["id"]

            voices = await client.get(f"/api/v1/voices?model_id={model_id}")
            assert voices.status_code == 200
            assert voices.json() == [
                {
                    "id": "core-contract-voice",
                    "label": "Core Contract Voice",
                    "capabilities": ["preset"],
                }
            ]
            generated = await client.post(
                "/api/v1/generations",
                json={
                    "model_id": model_id,
                    "voice_id": "core-contract-voice",
                    "text": "contract text",
                },
            )
            assert generated.status_code == 202, (
                generated.text
                + "\\n"
                + "\\n".join(
                    path.read_text(encoding="utf-8")
                    for path in (layout_root / "logs").glob("*.log")
                )
            )
            job = await app.state.generation_service.wait(generated.json()["id"])
            assert job.state.value == "completed", job.error
            artifact = app.state.generation_service.list_history()[0]
            with wave.open(
                str(app.state.generation_service.read_artifact(artifact.id)), "rb"
            ) as wav:
                assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (
                    48_000,
                    1,
                    2,
                )

            cancelled_response = await client.post(
                "/api/v1/generations",
                json={
                    "model_id": model_id,
                    "voice_id": "core-contract-voice",
                    "text": "cancel " * 1000,
                },
            )
            assert cancelled_response.status_code == 202, cancelled_response.text
            cancelled_id = cancelled_response.json()["id"]
            await _wait_for_generation_state(
                app.state.generation_service, cancelled_id, "generating"
            )
            await app.state.generation_service.cancel(cancelled_id)
            cancelled = await app.state.generation_service.wait(cancelled_id)
            assert cancelled.state.value == "cancelled"
            assert cancelled.artifact_id is None
            assert not tuple((layout_root / "staging").glob(".generation-*"))

            privacy_marker = layout_root / "run" / "privacy-worker-error.txt"
            privacy_marker.write_text(privacy_diagnostics, encoding="utf-8")
            try:
                worker_error = await client.get(f"/api/v1/voices?model_id={model_id}")
            finally:
                privacy_marker.unlink(missing_ok=True)
            assert worker_error.status_code == 503
            worker_error_json = worker_error.json()
            assert worker_error_json["error"]["details"] == {}

            invalid_input = await client.post(
                "/api/v1/models/validate",
                json={"repository_id": f"{secret} {url} {absolute_path} {traceback_text}"},
            )
            assert invalid_input.status_code == 422
            assert invalid_input.json()["error"]["details"] == {"field": "repository_id"}

            with pytest.raises(UnsafeEventPayloadError):
                app.state.event_store.append(
                    "download.failed",
                    {"job_id": "privacy-sentinel", "message": privacy_diagnostics},
                )

            await app.state.model_service.remove_model(model_id)
            assert not tuple((layout_root / "models").iterdir())

            public_json = json.dumps(
                {
                    "validation": validation.json(),
                    "voices": voices.json(),
                    "job": generated.json(),
                    "worker_error": worker_error_json,
                    "invalid_input": invalid_input.json(),
                }
            )
            with sqlite3.connect(layout_root / "database" / "tts-studio.sqlite3") as connection:
                rows = connection.execute(
                    "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view')"
                ).fetchall()
                database_values: list[str] = []
                for name, _ in rows:
                    values = repr(connection.execute(f'SELECT * FROM "{name}"').fetchall())
                    database_values.append(values)
                database_dump = "\n".join(database_values)
            durable_events = json.dumps(
                [event.public_data() for event in app.state.event_store.read_after(0).events]
            )
            logs = "\n".join(
                path.read_text(encoding="utf-8") for path in (layout_root / "logs").glob("*.log")
            )
            for sentinel in (secret, url, absolute_path, traceback_text):
                assert sentinel not in database_dump
                assert sentinel not in durable_events
                assert sentinel not in public_json
                assert sentinel not in logs
            assert str(layout_root) not in public_json
    finally:
        await supervisor.stop_all()


@pytest.mark.asyncio
async def test_authenticated_vieneu_contract_and_core_wav_path(tmp_path: Path) -> None:
    _make_cache(tmp_path)
    async with _worker_server(tmp_path) as channel:
        stub = engine_pb2_grpc.EngineWorkerStub(channel)
        metadata = (("x-tts-worker-token", TOKEN),)
        description = await stub.Describe(engine_pb2.DescribeRequest(), metadata=metadata)
        assert description.engine_id == "vieneu"
        assert {item.name for item in description.capabilities if item.supported} == (
            set(CAPABILITIES) - {"reference_cloning"}
        )
        health = await stub.Health(engine_pb2.HealthRequest(), metadata=metadata)
        assert health.status == engine_pb2.HealthResponse.READY
        loaded = await stub.LoadModel(
            engine_pb2.LoadModelRequest(
                model_id=MODEL_ID, cache_path="models/contract", variant="fp32"
            ),
            metadata=metadata,
        )
        assert loaded.loaded
        loaded_description = await stub.Describe(engine_pb2.DescribeRequest(), metadata=metadata)
        assert "reference_cloning" in {
            item.name for item in loaded_description.capabilities if item.supported
        }
        voices = await stub.ListVoices(
            engine_pb2.ListVoicesRequest(model_id=MODEL_ID), metadata=metadata
        )
        assert [(voice.label, voice.id) for voice in voices.voices] == [
            ("Contract Voice", "contract-voice")
        ]
        events = [
            event
            async for event in stub.Synthesize(
                engine_pb2.SynthesizeRequest(
                    model_id=MODEL_ID, voice_id="contract-voice", text="contract text"
                ),
                metadata=metadata,
            )
        ]
        assert events[0].HasField("header")
        assert events[0].header.sample_rate_hz == 48_000
        assert events[-1].HasField("result")
        assert events[-1].result.total_frames == 4

        reference_path = tmp_path / "staging" / "references" / "contract-reference.wav"
        reference_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(reference_path), "wb") as reference:
            reference.setnchannels(1)
            reference.setsampwidth(2)
            reference.setframerate(16_000)
            reference.writeframes(b"\x00\x00" * 1_600)
        validation = await stub.ValidateReference(
            engine_pb2.ValidateReferenceRequest(
                model_id=MODEL_ID,
                reference_path="staging/references/contract-reference.wav",
                transcript="reference transcript",
            ),
            metadata=metadata,
        )
        assert validation.valid
        assert validation.metadata.duration_ms == 100
        reference_events = [
            event
            async for event in stub.Synthesize(
                engine_pb2.SynthesizeRequest(
                    model_id=MODEL_ID,
                    reference=engine_pb2.ReferenceAudio(
                        reference_path="staging/references/contract-reference.wav",
                        transcript="reference transcript",
                    ),
                    text="contract text",
                ),
                metadata=metadata,
            )
        ]
        assert reference_events[-1].result.total_frames == 4
        unloaded = await stub.UnloadModel(
            engine_pb2.UnloadModelRequest(model_id=MODEL_ID), metadata=metadata
        )
        assert unloaded.unloaded
