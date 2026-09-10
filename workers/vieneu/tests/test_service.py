import asyncio
import importlib
import importlib.abc
import shutil
import sys
import threading
import types
import wave
from pathlib import Path

import grpc
import numpy as np
import pytest
import tts_studio_vieneu_worker.runtime as runtime_module
from tts_studio_protocol.engine.v1 import engine_pb2
from tts_studio_vieneu_worker.model_service import _CLONING_FILES, _CODEC_FILES, _GRAPH_FILES
from tts_studio_worker_sdk.auth import require_worker_token


class _Context:
    def __init__(self, token: str) -> None:
        self._token = token

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return (("x-tts-worker-token", self._token),)

    async def abort(self, code: grpc.StatusCode, details: str) -> None:
        raise RuntimeError((code, details))


class _CancelAfterFirstChunk(_Context):
    def __init__(self, token: str) -> None:
        super().__init__(token)
        self.checks = 0

    def cancelled(self) -> bool:
        self.checks += 1
        return self.checks > 1

    def time_remaining(self):
        return None


class _DeadlineContext(_Context):
    def cancelled(self) -> bool:
        return False

    def time_remaining(self):
        return 0.0


class _DisconnectContext(_Context):
    def __init__(self, token: str) -> None:
        super().__init__(token)
        self._done_callback = None
        self.disconnected = False

    def add_done_callback(self, callback) -> None:
        self._done_callback = callback

    def cancelled(self) -> bool:
        return self.disconnected

    def time_remaining(self):
        return None

    def disconnect(self) -> None:
        self.disconnected = True
        assert self._done_callback is not None
        self._done_callback(None)


class _DeadlineDisconnectContext(_DisconnectContext):
    def time_remaining(self):
        return 0.0


def _worker_type():
    return importlib.import_module(
        "tts_studio_vieneu_worker.service"
    ).VieNeuEngineWorker


class _FakeVieneu:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.released = False

    def list_preset_voices(self):
        return [("Minh Quân", "minh-quan")]

    def release(self):
        self.released = True

    def infer_stream(self, text, *, voice):
        assert text == "hello"
        assert voice == "minh-quan"
        yield np.array([0.0, 1.0], dtype=np.float32)
        yield np.array([-1.0], dtype=np.float32)


class _ReferenceVieneu(_FakeVieneu):
    def infer_stream(self, text, **kwargs):
        assert text == "hello"
        assert kwargs["ref_audio"].endswith("/reference.wav")
        assert "/staging/references/" not in kwargs["ref_audio"]
        assert kwargs["ref_text"] == "sample"
        yield np.array([0.0, 1.0], dtype=np.float32)


class _MismatchedReferenceVieneu(_FakeVieneu):
    def infer_stream(self, text, *, voice):
        yield np.array([0.0, 1.0], dtype=np.float32)


class _BrokenAudioVieneu(_FakeVieneu):
    def infer_stream(self, text, *, voice):
        yield np.array([[0.0]], dtype=np.float32)


class _FailingVieneu(_FakeVieneu):
    def infer_stream(self, text, *, voice):
        raise RuntimeError("sdk details must not escape")


class _EmptyVieneu(_FakeVieneu):
    def infer_stream(self, text, *, voice):
        yield np.array([], dtype=np.float32)


class _BlockingVieneu(_FakeVieneu):
    started = threading.Event()
    inference_release = threading.Event()
    finished = threading.Event()

    def infer_stream(self, text, *, voice):
        type(self).started.set()
        type(self).inference_release.wait()
        type(self).finished.set()
        yield np.array([0.0, 1.0], dtype=np.float32)


class _UncooperativeVieneu(_FakeVieneu):
    started = threading.Event()
    never_release = threading.Event()

    def infer_stream(self, text, *, voice):
        type(self).started.set()
        type(self).never_release.wait()
        yield np.array([0.0, 1.0], dtype=np.float32)


def _installed_model(root: Path) -> None:
    for variant in ("int8", "fp32"):
        for name in _GRAPH_FILES:
            path = root / "models" / "one" / "backbone" / variant / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"model")
    for name in _CODEC_FILES:
        path = root / "models" / "one" / "codec" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"codec")
    for name in _CLONING_FILES:
        path = root / "models" / "one" / "cloning" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"cloning")


@pytest.mark.asyncio
async def test_describe_reports_vieneu_foundation_contract(tmp_path: Path) -> None:
    worker = _worker_type()("secret", tmp_path)

    response = await worker.Describe(engine_pb2.DescribeRequest(), _Context("secret"))

    assert response.engine_id == "vieneu"
    assert response.protocol.major == 1
    assert response.engine_version == "3.6.3"
    assert response.max_concurrency == 1
    assert {cap.name for cap in response.capabilities if cap.supported} == {
        "health",
        "model_validation",
        "model_download",
        "model_lifecycle",
        "preset_voices",
        "streaming_synthesis",
        "synthesis_cancellation",
    }
    assert not response.HasField("alignment")


@pytest.mark.asyncio
async def test_align_reports_capability_unavailable_after_authentication(tmp_path: Path) -> None:
    worker = _worker_type()("secret", tmp_path)

    response = await worker.Align(
        engine_pb2.AlignRequest(model_id="model-1", audio_path="audio.wav", transcript="hello"),
        _Context("secret"),
    )

    assert response.error.code == "alignment_unavailable"
    assert response.error.message == "VieNeu alignment is unavailable"
    assert response.error.retryable is False
    assert response.error.details["adapter"] == "vieneu"


@pytest.mark.asyncio
async def test_describe_does_not_probe_sdk_signature_before_model_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_if_probed() -> bool:
        raise AssertionError("the SDK must not be imported or inspected before load")

    monkeypatch.setattr(runtime_module, "_sdk_reference_signature_supported", fail_if_probed)
    worker = _worker_type()("secret", tmp_path)

    response = await worker.Describe(engine_pb2.DescribeRequest(), _Context("secret"))

    assert "reference_cloning" not in {cap.name for cap in response.capabilities if cap.supported}


@pytest.mark.asyncio
async def test_describe_reports_reference_capability_after_runtime_load(tmp_path: Path) -> None:
    _installed_model(tmp_path)
    worker = _worker_type()("secret", tmp_path, vieneu_factory=_ReferenceVieneu)

    await worker.LoadModel(
        engine_pb2.LoadModelRequest(model_id="model-1", cache_path="models/one", variant="int8"),
        _Context("secret"),
    )
    response = await worker.Describe(engine_pb2.DescribeRequest(), _Context("secret"))

    assert "reference_cloning" in {cap.name for cap in response.capabilities if cap.supported}


def test_worker_startup_recovers_abandoned_download_scratch_safely(tmp_path: Path) -> None:
    scratch = tmp_path / ".vieneu-downloads"
    abandoned = scratch / ".repository-download-model-123"
    abandoned.mkdir(parents=True)
    (abandoned / "partial.bin").write_bytes(b"partial")
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "keep.bin"
    protected.write_bytes(b"keep")
    try:
        (scratch / "redirected").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")

    _worker_type()("secret", tmp_path, vieneu_factory=_FakeVieneu)

    assert not abandoned.exists()
    assert not (scratch / "redirected").exists()
    assert protected.read_bytes() == b"keep"


def test_worker_startup_preserves_a_replaced_scratch_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / ".vieneu-downloads"
    abandoned = scratch / ".repository-download-model-123"
    abandoned.mkdir(parents=True)
    (abandoned / "partial.bin").write_bytes(b"old")
    original_lstat = Path.lstat
    original_rmtree = shutil.rmtree
    replaced = False

    def replace_after_snapshot(path: Path):
        nonlocal replaced
        metadata = original_lstat(path)
        if path == abandoned and not replaced:
            replaced = True
            original_rmtree(path)
            path.mkdir()
            (path / "replacement.bin").write_bytes(b"keep")
        return metadata

    monkeypatch.setattr(Path, "lstat", replace_after_snapshot)

    from tts_studio_vieneu_worker.recovery import recover_download_scratch

    recover_download_scratch(tmp_path)

    assert replaced
    assert (abandoned / "replacement.bin").read_bytes() == b"keep"


@pytest.mark.asyncio
async def test_describe_omits_reference_capability_for_mismatched_sdk_signature(
    tmp_path: Path,
) -> None:
    worker = _worker_type()("secret", tmp_path, vieneu_factory=_MismatchedReferenceVieneu)

    response = await worker.Describe(engine_pb2.DescribeRequest(), _Context("secret"))

    assert "reference_cloning" not in {cap.name for cap in response.capabilities if cap.supported}


@pytest.mark.asyncio
async def test_invalid_token_is_rejected() -> None:
    with pytest.raises(RuntimeError) as error:
        await require_worker_token(_Context("wrong"), "secret")

    assert error.value.args[0][0] == grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_worker_import_and_lifecycle_do_not_import_or_construct_sdk(
    tmp_path: Path, monkeypatch
) -> None:
    class _ImportGuard(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname: str, path=None, target=None):
            if fullname == "vieneu" or fullname.startswith("vieneu."):
                raise AssertionError("VieNeu SDK must not be imported")

    constructed = False

    class _GuardedVieneu:
        def __init__(self, *args, **kwargs):
            nonlocal constructed
            constructed = True
            raise AssertionError("VieNeu SDK must not be constructed")

    service_name = "tts_studio_vieneu_worker.service"
    for module_name in tuple(sys.modules):
        if module_name == "vieneu" or module_name.startswith("vieneu."):
            monkeypatch.delitem(sys.modules, module_name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_ImportGuard(), *sys.meta_path])
    service_module = importlib.import_module(service_name)

    sdk_module = types.ModuleType("vieneu")
    sdk_module.Vieneu = _GuardedVieneu
    monkeypatch.setitem(sys.modules, "vieneu", sdk_module)
    worker_type = importlib.reload(service_module).VieNeuEngineWorker
    worker = worker_type("secret", tmp_path)

    describe = await worker.Describe(engine_pb2.DescribeRequest(), _Context("secret"))
    response = await worker.Health(engine_pb2.HealthRequest(), _Context("secret"))

    assert describe.engine_id == "vieneu"
    assert response.status == engine_pb2.HealthResponse.READY
    assert not constructed


@pytest.mark.asyncio
async def test_model_rpcs_fail_closed_until_a_model_is_loaded(tmp_path: Path) -> None:
    worker = _worker_type()("secret", tmp_path)

    validation = await worker.ValidateModel(
        engine_pb2.ValidateModelRequest(repository_id="example/model"), _Context("secret")
    )
    load = await worker.LoadModel(engine_pb2.LoadModelRequest(), _Context("secret"))
    unload = await worker.UnloadModel(engine_pb2.UnloadModelRequest(), _Context("secret"))
    voices = await worker.ListVoices(engine_pb2.ListVoicesRequest(), _Context("secret"))

    assert validation.error.code == "model_incompatible"
    assert load.error.code == "invalid_request"
    assert unload.error.code == "model_not_loaded"
    assert voices.error.code == "model_not_loaded"


@pytest.mark.asyncio
async def test_model_lifecycle_rpcs_load_and_discover_runtime_presets(tmp_path: Path) -> None:
    _installed_model(tmp_path)
    worker = _worker_type()("secret", tmp_path, vieneu_factory=_FakeVieneu)

    loaded = await worker.LoadModel(
        engine_pb2.LoadModelRequest(model_id="model-1", cache_path="models/one", variant="int8"),
        _Context("secret"),
    )
    voices = await worker.ListVoices(engine_pb2.ListVoicesRequest(model_id="model-1"), _Context("secret"))
    unloaded = await worker.UnloadModel(
        engine_pb2.UnloadModelRequest(model_id="model-1"), _Context("secret")
    )

    assert loaded.loaded and not loaded.HasField("error")
    assert [(voice.id, voice.label, list(voice.capabilities)) for voice in voices.voices] == [
        ("minh-quan", "Minh Quân", ["preset"])
    ]
    assert unloaded.unloaded and not unloaded.HasField("error")


@pytest.mark.asyncio
async def test_failed_unload_keeps_strict_worker_runtime_owned_before_next_load(
    tmp_path: Path,
) -> None:
    class _BrokenCleanupVieneu(_FakeVieneu):
        def release(self):
            raise OSError("runtime cleanup details must stay local")

    _installed_model(tmp_path)
    worker = _worker_type()("secret", tmp_path, vieneu_factory=_BrokenCleanupVieneu)
    request = engine_pb2.LoadModelRequest(
        model_id="model-1", cache_path="models/one", variant="int8"
    )

    loaded = await worker.LoadModel(request, _Context("secret"))
    unload_failed = await worker.UnloadModel(
        engine_pb2.UnloadModelRequest(model_id="model-1"), _Context("secret")
    )
    second_load = await worker.LoadModel(request, _Context("secret"))

    assert loaded.loaded
    assert unload_failed.error.code == "model_unload_failed"
    assert second_load.error.code == "model_already_loaded"


@pytest.mark.asyncio
async def test_download_and_synthesis_fail_closed_until_runtime_is_implemented(
    tmp_path: Path,
) -> None:
    worker = _worker_type()("secret", tmp_path)

    download_events = [
        event
        async for event in worker.DownloadModel(
            engine_pb2.DownloadModelRequest(), _Context("secret")
        )
    ]
    synthesis_events = [
        event
        async for event in worker.Synthesize(
            engine_pb2.SynthesizeRequest(), _Context("secret")
        )
    ]

    assert download_events[0].error.code == "invalid_request"
    assert synthesis_events[0].error.code == "model_not_loaded"


@pytest.mark.asyncio
async def test_direct_synthesis_rejects_unsupported_options(tmp_path: Path) -> None:
    _installed_model(tmp_path)
    worker = _worker_type()("secret", tmp_path, vieneu_factory=_FakeVieneu)
    await worker.LoadModel(
        engine_pb2.LoadModelRequest(model_id="model-1", cache_path="models/one", variant="int8"),
        _Context("secret"),
    )

    events = [
        event
        async for event in worker.Synthesize(
            engine_pb2.SynthesizeRequest(
                model_id="model-1", voice_id="minh-quan", text="hello",
                options=engine_pb2.SynthesisOptions(speed=1.0),
            ),
            _Context("secret"),
        )
    ]

    assert events[0].error.code == "option_unsupported"


async def test_synthesis_streams_strict_pcm_events_and_exact_result(tmp_path: Path) -> None:
    _installed_model(tmp_path)
    worker = _worker_type()("secret", tmp_path, vieneu_factory=_FakeVieneu)
    await worker.LoadModel(
        engine_pb2.LoadModelRequest(model_id="model-1", cache_path="models/one", variant="int8"),
        _Context("secret"),
    )

    events = [
        event
        async for event in worker.Synthesize(
            engine_pb2.SynthesizeRequest(model_id="model-1", voice_id="minh-quan", text="hello"),
            _Context("secret"),
        )
    ]

    assert [event.WhichOneof("payload") for event in events] == [
        "header", "chunk", "chunk", "progress", "result"
    ]
    assert events[0].header == engine_pb2.AudioHeader(sample_rate_hz=48000, channels=1, sample_format=1)
    assert [event.chunk.sequence for event in events if event.HasField("chunk")] == [0, 1]
    assert events[-1].result.total_frames == sum(len(event.chunk.pcm) for event in events if event.HasField("chunk")) // 2
    assert events[-1].result.duration_ms == round(events[-1].result.total_frames / 48)


@pytest.mark.asyncio
async def test_reference_synthesis_passes_reference_without_voice_or_style(tmp_path: Path) -> None:
    _installed_model(tmp_path)
    reference = tmp_path / "staging" / "references" / "ref.wav"
    reference.parent.mkdir(parents=True)
    with wave.open(str(reference), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\x00\x00" * 1600)
    worker = _worker_type()("secret", tmp_path, vieneu_factory=_ReferenceVieneu)
    await worker.LoadModel(
        engine_pb2.LoadModelRequest(model_id="model-1", cache_path="models/one", variant="int8"),
        _Context("secret"),
    )

    events = [
        event
        async for event in worker.Synthesize(
            engine_pb2.SynthesizeRequest(
                model_id="model-1",
                text="hello",
                reference=engine_pb2.ReferenceAudio(
                    reference_path="staging/references/ref.wav", transcript="sample"
                ),
            ),
            _Context("secret"),
        )
    ]

    assert events[-1].HasField("result")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("factory", "code"),
    [(_BrokenAudioVieneu, "invalid_audio"), (_FailingVieneu, "synthesis_failed")],
)
async def test_synthesis_maps_sdk_and_audio_failures_without_raw_exceptions(
    tmp_path: Path, factory, code: str
) -> None:
    _installed_model(tmp_path)
    worker = _worker_type()("secret", tmp_path, vieneu_factory=factory)
    await worker.LoadModel(
        engine_pb2.LoadModelRequest(model_id="model-1", cache_path="models/one", variant="int8"),
        _Context("secret"),
    )

    events = [
        event
        async for event in worker.Synthesize(
            engine_pb2.SynthesizeRequest(model_id="model-1", voice_id="minh-quan", text="hello"),
            _Context("secret"),
        )
    ]

    assert events[-1].error.code == code
    assert "sdk details" not in events[-1].error.message


@pytest.mark.asyncio
async def test_unknown_voice_and_empty_output_are_terminal_validation_errors(tmp_path: Path) -> None:
    _installed_model(tmp_path)
    worker = _worker_type()("secret", tmp_path, vieneu_factory=_FakeVieneu)
    await worker.LoadModel(
        engine_pb2.LoadModelRequest(model_id="model-1", cache_path="models/one", variant="int8"),
        _Context("secret"),
    )

    unknown = [
        event
        async for event in worker.Synthesize(
            engine_pb2.SynthesizeRequest(model_id="model-1", voice_id="missing", text="hello"),
            _Context("secret"),
        )
    ]
    assert [event.WhichOneof("payload") for event in unknown] == ["error"]
    assert unknown[0].error.code == "voice_not_found"

    empty_worker = _worker_type()("secret", tmp_path, vieneu_factory=_EmptyVieneu)
    await empty_worker.LoadModel(
        engine_pb2.LoadModelRequest(model_id="model-1", cache_path="models/one", variant="int8"),
        _Context("secret"),
    )
    empty = [
        event
        async for event in empty_worker.Synthesize(
            engine_pb2.SynthesizeRequest(model_id="model-1", voice_id="minh-quan", text="hello"),
            _Context("secret"),
        )
    ]
    assert [event.WhichOneof("payload") for event in empty] == ["error"]
    assert empty[0].error.code == "invalid_audio"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("context_type", "code"),
    [(_CancelAfterFirstChunk, "synthesis_cancelled"), (_DeadlineContext, "synthesis_deadline_exceeded")],
)
async def test_synthesis_terminates_with_structured_cancellation_or_deadline(
    tmp_path: Path, context_type, code: str
) -> None:
    _installed_model(tmp_path)
    worker = _worker_type()("secret", tmp_path, vieneu_factory=_FakeVieneu)
    await worker.LoadModel(
        engine_pb2.LoadModelRequest(model_id="model-1", cache_path="models/one", variant="int8"),
        _Context("secret"),
    )

    events = [
        event
        async for event in worker.Synthesize(
            engine_pb2.SynthesizeRequest(model_id="model-1", voice_id="minh-quan", text="hello"),
            context_type("secret"),
        )
    ]

    assert events[-1].error.code == code
    assert not any(event.HasField("result") for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("context_type", "code"),
    [(_DisconnectContext, "synthesis_cancelled"), (_DeadlineDisconnectContext, "synthesis_deadline_exceeded")],
)
async def test_termination_wakes_blocked_synthesis_and_preserves_busy_slot_until_unblocked(
    tmp_path: Path, context_type, code: str
) -> None:
    _installed_model(tmp_path)
    _BlockingVieneu.started.clear()
    _BlockingVieneu.inference_release.clear()
    _BlockingVieneu.finished.clear()
    worker = _worker_type()("secret", tmp_path, vieneu_factory=_BlockingVieneu)
    await worker.LoadModel(
        engine_pb2.LoadModelRequest(model_id="model-1", cache_path="models/one", variant="int8"),
        _Context("secret"),
    )
    context = context_type("secret")
    stream = worker.Synthesize(
        engine_pb2.SynthesizeRequest(model_id="model-1", voice_id="minh-quan", text="hello"),
        context,
    )

    first_event = asyncio.create_task(anext(stream))
    await asyncio.to_thread(_BlockingVieneu.started.wait, 1.0)
    assert not first_event.done()
    context.disconnect()
    _BlockingVieneu.inference_release.set()

    terminal = await asyncio.wait_for(first_event, timeout=1.0)
    assert terminal.error.code == code
    with pytest.raises(StopAsyncIteration):
        await anext(stream)

    await asyncio.to_thread(_BlockingVieneu.finished.wait, 1.0)
    assert worker._runtime._synthesis_lock.acquire(timeout=1.0)
    worker._runtime._synthesis_lock.release()
    reused = [
        event
        async for event in worker.Synthesize(
            engine_pb2.SynthesizeRequest(model_id="model-1", voice_id="minh-quan", text="hello"),
            _Context("secret"),
        )
    ]
    assert reused[-1].HasField("result")


@pytest.mark.asyncio
async def test_cancelled_synthesis_is_quiescent_before_unload_returns(tmp_path: Path) -> None:
    _installed_model(tmp_path)
    _BlockingVieneu.started.clear()
    _BlockingVieneu.inference_release.clear()
    _BlockingVieneu.finished.clear()
    worker = _worker_type()("secret", tmp_path, vieneu_factory=_BlockingVieneu)
    await worker.LoadModel(
        engine_pb2.LoadModelRequest(model_id="model-1", cache_path="models/one", variant="int8"),
        _Context("secret"),
    )
    context = _DisconnectContext("secret")
    stream = worker.Synthesize(
        engine_pb2.SynthesizeRequest(model_id="model-1", voice_id="minh-quan", text="hello"),
        context,
    )
    first_event = asyncio.create_task(anext(stream))
    await asyncio.to_thread(_BlockingVieneu.started.wait, 1.0)
    context.disconnect()

    async def release_inference() -> None:
        await asyncio.sleep(0.05)
        _BlockingVieneu.inference_release.set()

    release_task = asyncio.create_task(release_inference())
    terminal = await asyncio.wait_for(first_event, timeout=1.0)
    await release_task
    assert terminal.error.code == "synthesis_cancelled"
    assert _BlockingVieneu.finished.is_set()
    unloaded = await worker.UnloadModel(
        engine_pb2.UnloadModelRequest(model_id="model-1"), _Context("secret")
    )
    assert unloaded.unloaded
    assert _BlockingVieneu.finished.is_set()


@pytest.mark.asyncio
async def test_uncooperative_inference_has_bounded_cancellation_and_quarantines_runtime(
    tmp_path: Path,
) -> None:
    _installed_model(tmp_path)
    _UncooperativeVieneu.started.clear()
    _UncooperativeVieneu.never_release.clear()
    worker = _worker_type()("secret", tmp_path, vieneu_factory=_UncooperativeVieneu)
    await worker.LoadModel(
        engine_pb2.LoadModelRequest(model_id="model-1", cache_path="models/one", variant="int8"),
        _Context("secret"),
    )
    context = _DisconnectContext("secret")
    stream = worker.Synthesize(
        engine_pb2.SynthesizeRequest(model_id="model-1", voice_id="minh-quan", text="hello"),
        context,
    )
    first_event = asyncio.create_task(anext(stream))
    await asyncio.to_thread(_UncooperativeVieneu.started.wait, 1.0)
    context.disconnect()

    terminal = await asyncio.wait_for(first_event, timeout=1.0)
    assert terminal.error.code == "synthesis_cancelled"
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), timeout=1.0)

    unloaded = await worker.UnloadModel(
        engine_pb2.UnloadModelRequest(model_id="model-1"), _Context("secret")
    )
    assert unloaded.error.code == "model_unload_failed"
    assert worker._runtime._synthesis_stuck is True


@pytest.mark.asyncio
async def test_next_item_cancels_and_awaits_children_when_consumer_is_cancelled() -> None:
    service = importlib.import_module("tts_studio_vieneu_worker.service")
    queue: asyncio.Queue[tuple[str, object]] = asyncio.Queue()
    cancellation_wakeup = asyncio.get_running_loop().create_future()
    tasks_before = asyncio.all_tasks()

    consumer = asyncio.create_task(service._next_item(queue, cancellation_wakeup))
    await asyncio.sleep(0)
    consumer.cancel()

    with pytest.raises(asyncio.CancelledError):
        await consumer
    await asyncio.sleep(0)

    leaked = [
        task
        for task in asyncio.all_tasks()
        if task not in tasks_before and task is not asyncio.current_task() and not task.done()
    ]
    assert leaked == []
