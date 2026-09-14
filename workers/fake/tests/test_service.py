import wave

import pytest
from tts_studio_fake_worker import main as main_module
from tts_studio_fake_worker.service import FakeEngineWorker
from tts_studio_protocol.engine.v1 import engine_pb2


class _AuthenticatedContext:
    def __init__(self, token: str) -> None:
        self._token = token

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return (("x-tts-worker-token", self._token),)

    async def abort(self, *_: object) -> None:
        pytest.fail("authenticated worker request was aborted")

    def add_done_callback(self, callback) -> None:
        del callback

    def time_remaining(self):
        return None

    def cancelled(self) -> bool:
        return False


@pytest.fixture
def auth_context():
    return _AuthenticatedContext


@pytest.mark.asyncio
async def test_describe_reports_foundation_contract(auth_context) -> None:
    response = await FakeEngineWorker("secret").Describe(
        engine_pb2.DescribeRequest(), auth_context("secret")
    )
    assert response.engine_id == "fake"
    assert response.protocol.major == 1
    assert response.max_concurrency == 1
    assert response.engine_version == "0.2.0"
    assert [(capability.name, capability.supported) for capability in response.capabilities] == [
        ("health", True),
        ("model_validation", True),
        ("model_download", True),
        ("download_cancellation", True),
        ("model_lifecycle", True),
        ("preset_voices", True),
        ("streaming_synthesis", True),
        ("synthesis_cancellation", True),
        ("reference_cloning", True),
        ("alignment", True),
    ]
    assert response.alignment.units == ["word"]
    assert response.alignment.languages == ["und"]
    assert response.alignment.aligner == "fake-aligner/1.0"


@pytest.mark.asyncio
async def test_align_returns_deterministic_word_units_from_managed_wav(
    tmp_path, auth_context
) -> None:
    audio = tmp_path / "audio" / "artifact.wav"
    audio.parent.mkdir()
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48_000)
        stream.writeframes(b"\x00\x00" * 4_800)
    worker = FakeEngineWorker("secret", tmp_path / "staging")
    await worker.LoadModel(
        engine_pb2.LoadModelRequest(model_id="model-1", variant="int8"), auth_context("secret")
    )
    request = engine_pb2.AlignRequest(
        model_id="model-1", audio_path="audio/artifact.wav", transcript="hello world"
    )

    first = await worker.Align(request, auth_context("secret"))
    second = await worker.Align(request, auth_context("secret"))

    assert first == second
    assert first.result.schema_version == 1
    assert first.result.transcript == "hello world"
    assert (first.result.sample_rate_hz, first.result.total_frames) == (48_000, 4_800)
    assert first.result.unit == "word"
    assert first.result.aligner == "fake-aligner/1.0"
    assert [
        (unit.text, unit.source_start, unit.source_end, unit.start_frames, unit.end_frames)
        for unit in first.result.units
    ] == [
        ("hello", 0, 5, 0, 2_400),
        ("world", 6, 11, 2_400, 4_800),
    ]
    assert all(unit.estimated for unit in first.result.units)


@pytest.mark.asyncio
async def test_align_preserves_utf8_source_byte_offsets(tmp_path, auth_context) -> None:
    audio = tmp_path / "audio" / "artifact.wav"
    audio.parent.mkdir()
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48_000)
        stream.writeframes(b"\x00\x00" * 900)
    worker = FakeEngineWorker("secret", tmp_path / "staging")
    await worker.LoadModel(
        engine_pb2.LoadModelRequest(model_id="model-1", variant="int8"), auth_context("secret")
    )
    response = await worker.Align(
        engine_pb2.AlignRequest(
            model_id="model-1", audio_path="audio/artifact.wav", transcript="Xin chào thế giới"
        ),
        auth_context("secret"),
    )
    assert [(unit.text, unit.source_start, unit.source_end) for unit in response.result.units] == [
        ("Xin", 0, 3),
        ("chào", 4, 9),
        ("thế", 10, 15),
        ("giới", 16, 22),
    ]
    assert all(unit.estimated for unit in response.result.units)


@pytest.mark.asyncio
async def test_align_rejects_unsafe_audio_paths(tmp_path, auth_context) -> None:
    audio = tmp_path / "audio" / "artifact.wav"
    audio.parent.mkdir()
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48_000)
        stream.writeframes(b"\x00\x00" * 10)
    outside = tmp_path / "outside.wav"
    outside.write_bytes(audio.read_bytes())
    (tmp_path / "audio" / "link.wav").symlink_to(outside)
    worker = FakeEngineWorker("secret", tmp_path / "staging")
    await worker.LoadModel(
        engine_pb2.LoadModelRequest(model_id="model-1", variant="int8"), auth_context("secret")
    )
    for path in ("audio/../outside.wav", "audio\\artifact.wav", "audio/link.wav"):
        response = await worker.Align(
            engine_pb2.AlignRequest(model_id="model-1", audio_path=path, transcript="hello"),
            auth_context("secret"),
        )
        assert response.error.code == "alignment_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("transcript", ["bad\x00text", "x" * 2001, " ".join(["x"] * 10001)])
async def test_align_rejects_nul_and_bounded_requests(tmp_path, auth_context, transcript) -> None:
    audio = tmp_path / "audio" / "artifact.wav"
    audio.parent.mkdir()
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48_000)
        stream.writeframes(b"\x00\x00" * 10)
    worker = FakeEngineWorker("secret", tmp_path / "staging")
    await worker.LoadModel(
        engine_pb2.LoadModelRequest(model_id="model-1", variant="int8"), auth_context("secret")
    )
    response = await worker.Align(
        engine_pb2.AlignRequest(
            model_id="model-1", audio_path="audio/artifact.wav", transcript=transcript
        ),
        auth_context("secret"),
    )
    assert not response.HasField("result")
    assert response.error.code == "alignment_request_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [
        ("error", "alignment_failed"),
        ("timeout", "alignment_deadline_exceeded"),
        ("cancelled", "alignment_cancelled"),
    ],
)
async def test_align_test_controls_return_stable_failures(
    tmp_path, auth_context, mode, expected_code
) -> None:
    audio = tmp_path / "audio" / "artifact.wav"
    audio.parent.mkdir()
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48_000)
        stream.writeframes(b"\x00\x00" * 480)
    worker = FakeEngineWorker("secret", tmp_path / "staging", alignment_mode=mode)
    await worker.LoadModel(
        engine_pb2.LoadModelRequest(model_id="model-1", variant="int8"), auth_context("secret")
    )

    response = await worker.Align(
        engine_pb2.AlignRequest(
            model_id="model-1", audio_path="audio/artifact.wav", transcript="hello"
        ),
        auth_context("secret"),
    )

    assert not response.HasField("result")
    assert response.error.code == expected_code
    assert response.error.retryable is (mode == "timeout")


@pytest.mark.asyncio
async def test_align_malformed_control_returns_empty_units_for_core_validation(
    tmp_path, auth_context
) -> None:
    audio = tmp_path / "audio" / "artifact.wav"
    audio.parent.mkdir()
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48_000)
        stream.writeframes(b"\x00\x00" * 480)
    worker = FakeEngineWorker("secret", tmp_path / "staging", alignment_mode="malformed")
    await worker.LoadModel(
        engine_pb2.LoadModelRequest(model_id="model-1", variant="int8"), auth_context("secret")
    )
    response = await worker.Align(
        engine_pb2.AlignRequest(
            model_id="model-1", audio_path="audio/artifact.wav", transcript="hello"
        ),
        auth_context("secret"),
    )
    assert response.result.units == []


@pytest.mark.asyncio
async def test_validate_reference_requires_auth_and_returns_metadata(
    tmp_path, auth_context
) -> None:
    path = tmp_path / "staging" / "references" / "ref.wav"
    path.parent.mkdir(parents=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\x00\x00" * 1600)

    worker = FakeEngineWorker("secret", tmp_path / "staging")
    await worker.LoadModel(
        engine_pb2.LoadModelRequest(model_id="model-1", variant="int8"), auth_context("secret")
    )
    response = await worker.ValidateReference(
        engine_pb2.ValidateReferenceRequest(
            model_id="model-1", reference_path="staging/references/ref.wav", transcript="hello"
        ),
        auth_context("secret"),
    )

    assert response.valid
    assert response.metadata.sample_rate_hz == 16000
    assert response.metadata.channels == 1
    assert response.metadata.duration_ms == 100


@pytest.mark.asyncio
async def test_reference_synthesis_is_deterministic_and_consumes_file(
    tmp_path, auth_context
) -> None:
    reference_root = tmp_path / "staging"
    path = reference_root / "references" / "ref.wav"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"reference-bytes")
    worker = FakeEngineWorker("secret", reference_root)
    await worker.LoadModel(
        engine_pb2.LoadModelRequest(model_id="model-1", variant="int8"), auth_context("secret")
    )
    request = engine_pb2.SynthesizeRequest(
        model_id="model-1",
        text="hello",
        reference=engine_pb2.ReferenceAudio(
            reference_path="staging/references/ref.wav", transcript="sample"
        ),
    )

    first = [event async for event in worker.Synthesize(request, auth_context("secret"))]
    second = [event async for event in worker.Synthesize(request, auth_context("secret"))]

    assert [event.chunk.pcm for event in first if event.HasField("chunk")] == [
        event.chunk.pcm for event in second if event.HasField("chunk")
    ]


@pytest.mark.asyncio
async def test_reference_synthesis_rejects_redirected_component_during_open(
    tmp_path, auth_context, monkeypatch
) -> None:
    references = tmp_path / "staging" / "references"
    references.mkdir(parents=True)
    path = references / "ref.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\x00\x00" * 1600)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "ref.wav").write_bytes(b"outside")
    real_open = __import__("os").open
    swapped = False

    def swap_before_final_open(name, *args, **kwargs):
        nonlocal swapped
        if (
            not swapped
            and __import__("os").path.basename(__import__("os").fspath(name)) == "ref.wav"
        ):
            swapped = True
            references.rename(tmp_path / "references-original")
            references.symlink_to(outside, target_is_directory=True)
        return real_open(name, *args, **kwargs)

    monkeypatch.setattr("os.open", swap_before_final_open)
    worker = FakeEngineWorker("secret", tmp_path / "staging")
    await worker.LoadModel(
        engine_pb2.LoadModelRequest(model_id="model-1", variant="int8"), auth_context("secret")
    )
    response = await worker.ValidateReference(
        engine_pb2.ValidateReferenceRequest(
            model_id="model-1", reference_path="staging/references/ref.wav"
        ),
        auth_context("secret"),
    )

    assert response.valid


@pytest.mark.asyncio
async def test_health_reports_ready(auth_context) -> None:
    response = await FakeEngineWorker("secret").Health(
        engine_pb2.HealthRequest(), auth_context("secret")
    )
    assert response.status == engine_pb2.HealthResponse.READY


def test_entrypoint_uses_data_dir_for_staging(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("secret", encoding="utf-8")
    token_file.chmod(0o600)
    ready_file = tmp_path / "run" / "ready.json"
    ready_file.parent.mkdir()
    data_dir = tmp_path / "managed"
    data_dir.mkdir()
    observed = {}

    async def capture_worker(worker, **kwargs):
        del kwargs
        observed["staging_root"] = worker._models._staging_root

    monkeypatch.setattr(main_module, "serve_worker", capture_worker)
    monkeypatch.setattr(
        "sys.argv",
        [
            "tts-studio-fake-worker",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--token-file",
            str(token_file),
            "--ready-file",
            str(ready_file),
            "--data-dir",
            str(data_dir),
        ],
    )

    main_module.main()

    assert observed["staging_root"] == data_dir / "staging"
