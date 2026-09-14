import json
import sys
import threading
import types
import wave
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest
import tts_studio_vieneu_worker.runtime as runtime_module
from tts_studio_protocol.engine.v1 import engine_pb2
from tts_studio_vieneu_worker.model_service import _CLONING_FILES, _CODEC_FILES, _GRAPH_FILES
from tts_studio_vieneu_worker.runtime import RuntimeFailure, VieNeuRuntime


class FakeVieneu:
    instances: ClassVar[list[object]] = []

    def __init__(self, **kwargs):
        self.arguments = kwargs
        self.released = False
        type(self).instances.append(self)

    def list_preset_voices(self):
        return [("Minh Quân", "minh-quan"), ("Adam", "adam")]

    def release(self):
        self.released = True

    def infer_stream(self, text, *, voice):
        assert text == "hello"
        assert voice == "minh-quan"
        yield np.array([0.0, 1.0], dtype=np.float32)


class ReferenceVieneu(FakeVieneu):
    def infer_stream(self, text, **kwargs):
        assert text == "hello"
        assert kwargs["ref_audio"].endswith("/reference.wav")
        assert "/staging/references/" not in kwargs["ref_audio"]
        assert kwargs["ref_text"] == "sample"
        yield np.array([0.0, 1.0], dtype=np.float32)


class MismatchedReferenceVieneu(FakeVieneu):
    def infer_stream(self, text, *, voice):
        yield np.array([0.0, 1.0], dtype=np.float32)


class CloseOnlyVieneu:
    instances: ClassVar[list[object]] = []

    def __init__(self, **kwargs):
        self.arguments = kwargs
        type(self).instances.append(self)
        self.closed = False

    def close(self):
        self.closed = True


class NoCleanupVieneu:
    def __init__(self, **kwargs):
        self.arguments = kwargs

    def list_preset_voices(self):
        return []


class LazyReferenceFactory:
    @property
    def infer_stream(self):
        raise AssertionError("the SDK signature must not be inspected before load")

    def __call__(self, **kwargs):
        return ReferenceVieneu(**kwargs)


@pytest.fixture(autouse=True)
def reset_instances():
    FakeVieneu.instances.clear()


def model_files(root: Path) -> Path:
    cache = root / "models" / "installation"
    for variant in ("int8", "fp32"):
        for name in _GRAPH_FILES:
            path = cache / "backbone" / variant / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"model")
    for name in _CODEC_FILES:
        path = cache / "codec" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"codec")
    for name in _CLONING_FILES:
        path = cache / "cloning" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"cloning")
    return cache


def test_load_constructs_one_lazy_onnx_runtime_with_local_paths(tmp_path: Path) -> None:
    cache = model_files(tmp_path)
    runtime = VieNeuRuntime(tmp_path, FakeVieneu)

    runtime.load("model-1", "models/installation", "int8")

    assert FakeVieneu.instances[0].arguments == {
        "mode": "v3turbo",
        "backend": "onnx",
        "precision": "int8",
        "backbone_repo": str(tmp_path / "models" / "installation" / "cloning"),
        "onnx_dir": str(cache / "backbone" / "int8"),
        "codec_dir": str(cache / "codec"),
        "threads": 0,
    }


def test_reference_capability_is_lazy_until_a_runtime_is_loaded(tmp_path: Path) -> None:
    factory = LazyReferenceFactory()
    runtime = VieNeuRuntime(tmp_path, factory)

    assert runtime.reference_cloning_supported() is False

    model_files(tmp_path)
    runtime.load("model-1", "models/installation", "int8")

    assert runtime.reference_cloning_supported() is True


def test_load_uses_pinned_local_codec_without_hub_downloads(tmp_path: Path, monkeypatch) -> None:
    model_files(tmp_path)
    import huggingface_hub
    from vieneu._v3_turbo_engine import onnx_runtime_lite

    hub_calls: list[tuple[object, ...]] = []

    def fail_hub_download(*args, **kwargs):
        hub_calls.append((args, kwargs))
        raise AssertionError("activated model loading must not contact Hugging Face")

    class CapturingOnnxEngine:
        instances: ClassVar[list[object]] = []

        def __init__(self, **kwargs):
            self.arguments = kwargs
            self.babble_retries = 0
            type(self).instances.append(self)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fail_hub_download)
    monkeypatch.setattr(onnx_runtime_lite, "OnnxV3LiteEngine", CapturingOnnxEngine)

    VieNeuRuntime(tmp_path).load("model-1", "models/installation", "int8")

    assert hub_calls == []
    assert CapturingOnnxEngine.instances[0].arguments["codec_dir"] == str(
        tmp_path / "models" / "installation" / "codec"
    )
    assert CapturingOnnxEngine.instances[0].arguments["checkpoint_path"] == str(
        tmp_path / "models" / "installation" / "cloning"
    )


def test_load_with_real_sdk_uses_local_codec_and_cloning_assets_without_hub_downloads(
    tmp_path: Path, monkeypatch
) -> None:
    cache = model_files(tmp_path)
    config = {
        "n_vq": 1,
        "hidden_size": 2,
        "num_hidden_layers": 1,
        "audio_pad_token_id": 0,
        "text_prompt_start_token_id": 1,
        "text_prompt_end_token_id": 2,
        "speech_generation_start_token_id": 3,
        "speech_generation_end_token_id": 4,
        "audio_ref_slot_token_id": 5,
        "text_vocab_size": 8,
    }
    (cache / "backbone" / "int8" / "config.json").write_text(json.dumps(config))
    (cache / "backbone" / "int8" / "tokenizer.json").write_text("{}")

    import huggingface_hub
    import onnxruntime
    from vieneu._v3_turbo_engine import onnx_runtime_lite

    hub_calls: list[tuple[object, ...]] = []

    def fail_hub_download(*args, **kwargs):
        hub_calls.append((args, kwargs))
        raise AssertionError("activated model loading must not contact Hugging Face")

    class _SessionOptions:
        def add_session_config_entry(self, key: str, value: str) -> None:
            del key, value

    class _Session:
        def __init__(self, path: str, *args, **kwargs) -> None:
            del args, kwargs
            self.path = path

        def get_inputs(self):
            return [types.SimpleNamespace(name="input")]

        def get_outputs(self):
            return [types.SimpleNamespace(name="output")]

    class _FakeTokenizer:
        @classmethod
        def from_file(cls, path: str):
            del path
            return cls()

    class _Heads:
        def __getitem__(self, name: str):
            if name == "text_emb":
                return np.zeros((8, 2), dtype=np.float32)
            return np.zeros((1, 8, 2), dtype=np.float32)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fail_hub_download)
    monkeypatch.setattr(onnxruntime, "SessionOptions", _SessionOptions)
    monkeypatch.setattr(onnxruntime, "InferenceSession", _Session)
    monkeypatch.setattr(onnxruntime, "get_available_providers", lambda: ["CPUExecutionProvider"])
    monkeypatch.setattr(
        onnxruntime, "GraphOptimizationLevel", types.SimpleNamespace(ORT_ENABLE_ALL=1)
    )
    monkeypatch.setitem(sys.modules, "tokenizers", types.ModuleType("tokenizers"))
    sys.modules["tokenizers"].Tokenizer = _FakeTokenizer
    monkeypatch.setattr(onnx_runtime_lite.np, "load", lambda path: _Heads())

    runtime = runtime_module.VieNeuRuntime(tmp_path)
    runtime.load("model-1", "models/installation", "int8")
    sdk_engine = runtime.engine_for("model-1").engine
    assert hub_calls == []
    speaker_encoder = sdk_engine._ensure_speaker_encoder()

    assert speaker_encoder.onnx_path == str(cache / "cloning" / "speaker_encoder.onnx")
    assert sdk_engine.denoiser is not None
    assert sdk_engine.denoiser.sess.path == str(cache / "cloning" / "denoiser.onnx")
    assert sdk_engine.sess_codec_dec.path == str(
        cache / "codec" / "moss_audio_tokenizer_decode_full.onnx"
    )


def test_load_rejects_second_model_unknown_variant_and_unsafe_paths(tmp_path: Path) -> None:
    model_files(tmp_path)
    runtime = VieNeuRuntime(tmp_path, FakeVieneu)
    runtime.load("model-1", "models/installation", "int8")

    with pytest.raises(RuntimeFailure, match="already loaded"):
        runtime.load("model-2", "models/installation", "int8")
    with pytest.raises(RuntimeFailure):
        VieNeuRuntime(tmp_path, FakeVieneu).load("model-1", "models/installation", "unknown")
    with pytest.raises(RuntimeFailure):
        VieNeuRuntime(tmp_path, FakeVieneu).load("model-1", "../outside", "int8")
    for unsafe_path in ("C:/outside", "C:\\outside", "//server/share/model"):
        with pytest.raises(RuntimeFailure) as error:
            VieNeuRuntime(tmp_path, FakeVieneu).load("model-1", unsafe_path, "int8")
        assert error.value.code == "invalid_request"


def test_load_rejects_missing_files_and_symlinked_model_paths(tmp_path: Path) -> None:
    cache = model_files(tmp_path)
    (cache / "codec" / _CODEC_FILES[0]).unlink()
    with pytest.raises(RuntimeFailure):
        VieNeuRuntime(tmp_path, FakeVieneu).load("model-1", "models/installation", "int8")

    symlink_root = tmp_path / "symlink-root"
    redirected = symlink_root / "redirected"
    redirected.mkdir(parents=True)
    (symlink_root / "models").mkdir()
    (symlink_root / "models" / "installation").symlink_to(redirected, target_is_directory=True)
    with pytest.raises(RuntimeFailure):
        VieNeuRuntime(symlink_root, FakeVieneu).load("model-1", "models/installation", "int8")


def test_load_requires_both_runtime_variants(tmp_path: Path) -> None:
    cache = model_files(tmp_path)
    (cache / "backbone" / "fp32" / _GRAPH_FILES[0]).unlink()

    with pytest.raises(RuntimeFailure, match="unavailable"):
        VieNeuRuntime(tmp_path, FakeVieneu).load("model-1", "models/installation", "int8")


def test_load_requires_shared_cloning_assets(tmp_path: Path) -> None:
    cache = model_files(tmp_path)
    (cache / "cloning" / _CLONING_FILES[0]).unlink()

    with pytest.raises(RuntimeFailure, match="unavailable"):
        VieNeuRuntime(tmp_path, FakeVieneu).load("model-1", "models/installation", "int8")


def test_unload_releases_runtime_and_discovery_returns_worker_values(tmp_path: Path) -> None:
    model_files(tmp_path)
    runtime = VieNeuRuntime(tmp_path, FakeVieneu)
    runtime.load("model-1", "models/installation", "fp32")

    voices = runtime.list_voices("model-1")

    assert voices == (
        engine_pb2.PresetVoice(id="minh-quan", label="Minh Quân", capabilities=["preset"]),
        engine_pb2.PresetVoice(id="adam", label="Adam", capabilities=["preset"]),
    )
    assert runtime.unload("model-1") is None
    assert FakeVieneu.instances[0].released
    with pytest.raises(RuntimeFailure, match="not loaded"):
        runtime.engine_for("model-1")


def test_unload_uses_verified_close_cleanup_api(tmp_path: Path) -> None:
    model_files(tmp_path)
    runtime = VieNeuRuntime(tmp_path, CloseOnlyVieneu)
    runtime.load("model-1", "models/installation", "int8")

    runtime.unload("model-1")

    assert CloseOnlyVieneu.instances[0].closed


def test_unload_fails_closed_when_cleanup_method_raises(tmp_path: Path) -> None:
    class BrokenCleanup(FakeVieneu):
        def release(self):
            raise OSError("cleanup failed")

    model_files(tmp_path)
    runtime = VieNeuRuntime(tmp_path, BrokenCleanup)
    runtime.load("model-1", "models/installation", "int8")

    with pytest.raises(RuntimeFailure, match="released") as error:
        runtime.unload("model-1")

    assert error.value.code == "model_unload_failed"
    assert runtime.engine_for("model-1") is not None


def test_unload_fails_closed_when_sdk_has_no_cleanup_api(tmp_path: Path) -> None:
    model_files(tmp_path)
    runtime = VieNeuRuntime(tmp_path, NoCleanupVieneu)
    runtime.load("model-1", "models/installation", "int8")

    with pytest.raises(RuntimeFailure, match="released") as error:
        runtime.unload("model-1")

    assert error.value.code == "model_unload_failed"
    assert runtime.engine_for("model-1") is not None


def test_load_rejects_none_factory_result_and_allows_retry(tmp_path: Path) -> None:
    model_files(tmp_path)
    results = iter((None, FakeVieneu()))

    def factory(**kwargs):
        result = next(results)
        if result is not None:
            result.arguments = kwargs
        return result

    runtime = VieNeuRuntime(tmp_path, factory)
    with pytest.raises(RuntimeFailure, match="loaded"):
        runtime.load("model-1", "models/installation", "int8")

    runtime.load("model-1", "models/installation", "int8")
    assert runtime.engine_for("model-1") is not None


def test_load_maps_filesystem_oserror_to_runtime_failure(tmp_path: Path, monkeypatch) -> None:
    runtime = VieNeuRuntime(tmp_path, FakeVieneu)

    def denied_is_dir(self):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "is_dir", denied_is_dir)

    with pytest.raises(RuntimeFailure) as error:
        runtime.load("model-1", "models/installation", "int8")

    assert error.value.code == "model_load_failed"


def test_load_maps_path_valueerror_to_invalid_request(tmp_path: Path, monkeypatch) -> None:
    runtime = VieNeuRuntime(tmp_path, FakeVieneu)

    def invalid_absolute(self):
        raise ValueError("invalid path")

    monkeypatch.setattr(Path, "absolute", invalid_absolute)

    with pytest.raises(RuntimeFailure) as error:
        runtime.load("model-1", "models/installation", "int8")

    assert error.value.code == "invalid_request"


def test_synthesize_uses_only_voice_argument_and_releases_serialization_slot(
    tmp_path: Path,
) -> None:
    model_files(tmp_path)
    runtime = VieNeuRuntime(tmp_path, FakeVieneu)
    runtime.load("model-1", "models/installation", "int8")

    assert (
        next(runtime.synthesize("model-1", "minh-quan", "hello", threading.Event()))
        == b"\x00\x00\xff\x7f"
    )


def test_reference_synthesis_uses_only_reference_arguments(tmp_path: Path) -> None:
    model_files(tmp_path)
    reference = tmp_path / "staging" / "references" / "ref.wav"
    reference.parent.mkdir(parents=True)
    with wave.open(str(reference), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\x00\x00" * 1600)
    runtime = VieNeuRuntime(tmp_path, ReferenceVieneu)
    runtime.load("model-1", "models/installation", "int8")

    assert list(
        runtime.synthesize(
            "model-1",
            None,
            "hello",
            threading.Event(),
            reference_path="staging/references/ref.wav",
            transcript="sample",
        )
    ) == [b"\x00\x00\xff\x7f"]


def test_reference_signature_mismatch_is_structured_unsupported(tmp_path: Path) -> None:
    model_files(tmp_path)
    reference = tmp_path / "staging" / "references" / "ref.wav"
    reference.parent.mkdir(parents=True)
    reference.write_bytes(b"reference")
    runtime = VieNeuRuntime(tmp_path, MismatchedReferenceVieneu)
    runtime.load("model-1", "models/installation", "int8")

    with pytest.raises(RuntimeFailure) as error:
        runtime.validate_reference("model-1", "staging/references/ref.wav", "sample")

    assert error.value.code == "reference_unsupported"


def test_synthesize_rejects_concurrent_request_and_allows_retry(tmp_path: Path) -> None:
    model_files(tmp_path)
    runtime = VieNeuRuntime(tmp_path, FakeVieneu)
    runtime.load("model-1", "models/installation", "int8")

    first = runtime.synthesize("model-1", "minh-quan", "hello", threading.Event())
    next(first)
    second = runtime.synthesize("model-1", "minh-quan", "hello", threading.Event())
    with pytest.raises(RuntimeFailure, match="already synthesizing"):
        next(second)
    first.close()
    assert list(runtime.synthesize("model-1", "minh-quan", "hello", threading.Event()))


def test_synthesize_cancellation_releases_serialization_slot(tmp_path: Path) -> None:
    model_files(tmp_path)
    runtime = VieNeuRuntime(tmp_path, FakeVieneu)
    runtime.load("model-1", "models/installation", "int8")
    cancellation = threading.Event()
    stream = runtime.synthesize("model-1", "minh-quan", "hello", cancellation)

    next(stream)
    cancellation.set()
    with pytest.raises(StopIteration):
        next(stream)
    assert list(runtime.synthesize("model-1", "minh-quan", "hello", threading.Event()))
