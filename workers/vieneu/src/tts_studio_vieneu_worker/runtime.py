"""Lazy, path-confined VieNeu ONNX runtime lifecycle."""

from __future__ import annotations

import inspect
import os
import shutil
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from tts_studio_protocol.engine.v1 import engine_pb2

from .audio import waveform_to_pcm16
from .model_service import _CLONING_FILES, _CODEC_FILES, _GRAPH_FILES, _canonical_data_root
from .reference import ReferenceValidationError, VieNeuReferenceValidator

_VARIANTS = {"int8", "fp32"}


class RuntimeFailure(Exception):
    """An expected runtime failure with a safe Worker error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _sdk_factory(*args: object, **kwargs: object) -> Any:
    from vieneu import Vieneu

    codec_dir = kwargs.get("codec_dir")
    if kwargs.get("backend") != "onnx" or not isinstance(codec_dir, str):
        return Vieneu(*args, **kwargs)

    # VieNeu 3.6.3 accepts codec_dir in its public **kwargs but drops it before
    # constructing OnnxV3LiteEngine. Patch that one SDK seam for this call so
    # the engine receives the activated, validated codec tree instead of
    # falling back to its unpinned Hugging Face download path.
    from vieneu._v3_turbo_engine import onnx_runtime_lite

    sdk_engine = onnx_runtime_lite.OnnxV3LiteEngine

    class LocalCodecOnnxEngine(sdk_engine):
        def __init__(self, *engine_args: object, **engine_kwargs: object) -> None:
            engine_kwargs["codec_dir"] = codec_dir
            super().__init__(*engine_args, **engine_kwargs)

    onnx_runtime_lite.OnnxV3LiteEngine = LocalCodecOnnxEngine
    try:
        return Vieneu(*args, **kwargs)
    finally:
        onnx_runtime_lite.OnnxV3LiteEngine = sdk_engine


def _sdk_reference_signature_supported() -> bool:
    try:
        from vieneu.v3turbo import V3TurboVieNeuTTS

        target = getattr(V3TurboVieNeuTTS, "infer_stream", None)
        return target is not None and _supports_reference_signature(
            target, allow_verified_kwargs=True
        )
    except Exception:  # noqa: BLE001 - unavailable SDK means unavailable capability
        return False


def _supports_reference_signature(target: object, *, allow_verified_kwargs: bool = False) -> bool:
    try:
        signature = inspect.signature(target)
        parameter_names = set(signature.parameters)
        if not {"ref_audio", "ref_text"}.issubset(parameter_names) and not (
            allow_verified_kwargs
            and any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values())
        ):
            return False
        try:
            signature.bind("text", ref_audio="reference.wav", ref_text="transcript")
        except TypeError:
            signature.bind(None, "text", ref_audio="reference.wav", ref_text="transcript")
    except (TypeError, ValueError):
        return False
    return True


class VieNeuRuntime:
    """Own at most one loaded SDK instance for the life of a Worker."""

    def __init__(self, data_root: Path, vieneu_factory: Callable[..., Any] | None = None) -> None:
        self._data_root = _canonical_data_root(data_root)
        self._vieneu_factory = vieneu_factory or _sdk_factory
        self._model_id: str | None = None
        self._engine: Any | None = None
        self._synthesis_lock = threading.Lock()
        self._synthesis_stuck = False
        self._reference_validator = VieNeuReferenceValidator(self._data_root)

    @property
    def synthesis_stuck(self) -> bool:
        return self._synthesis_stuck

    def quarantine_synthesis(self) -> None:
        self._synthesis_stuck = True

    def load(self, model_id: str, cache_path: str, variant: str) -> None:
        if self._engine is not None:
            raise RuntimeFailure("model_already_loaded", "A model is already loaded")
        if not model_id:
            raise RuntimeFailure("invalid_request", "The model ID is invalid")
        if variant not in _VARIANTS:
            raise RuntimeFailure("variant_unsupported", "The requested variant is unsupported")
        try:
            cache = self._safe_cache_path(cache_path)
            onnx_dir = cache / "backbone" / variant
            codec_dir = cache / "codec"
            for runtime_variant in sorted(_VARIANTS):
                self._require_files(cache / "backbone" / runtime_variant, _GRAPH_FILES)
            self._require_files(codec_dir, _CODEC_FILES)
            self._require_files(cache / "cloning", _CLONING_FILES)
        except RuntimeFailure:
            raise
        except ValueError as error:
            raise RuntimeFailure("invalid_request", "The model cache path is invalid") from error
        except OSError as error:
            raise RuntimeFailure("model_load_failed", "The VieNeu model files are unavailable") from error
        try:
            engine = self._vieneu_factory(
                mode="v3turbo",
                backend="onnx",
                precision=variant,
                backbone_repo=str(cache / "cloning"),
                onnx_dir=str(onnx_dir),
                codec_dir=str(codec_dir),
                threads=0,
            )
        except Exception as error:
            raise RuntimeFailure("model_load_failed", "The VieNeu model could not be loaded") from error
        if engine is None:
            raise RuntimeFailure("model_load_failed", "The VieNeu model could not be loaded")
        self._engine = engine
        self._model_id = model_id

    def unload(self, model_id: str) -> None:
        self._require_model(model_id)
        if self._synthesis_stuck:
            raise RuntimeFailure(
                "model_unload_failed", "The VieNeu model cannot be released while synthesis is stuck"
            )
        # Unload must not race native inference.  Synthesis owns this lock for
        # the lifetime of its producer, so a direct lifecycle call waits until
        # cancellation has quiesced the SDK before releasing model resources.
        self._synthesis_lock.acquire()
        try:
            assert self._engine is not None
            engine = self._engine
            try:
                cleanup = getattr(engine, "release", None)
                if not callable(cleanup):
                    cleanup = getattr(engine, "close", None)
                if not callable(cleanup):
                    raise TypeError("no verified SDK cleanup API")
                cleanup()
            except Exception as error:
                raise RuntimeFailure("model_unload_failed", "The VieNeu model could not be released") from error
            self._engine = None
            self._model_id = None
        finally:
            self._synthesis_lock.release()

    def list_voices(self, model_id: str) -> tuple[engine_pb2.PresetVoice, ...]:
        engine = self.engine_for(model_id)
        try:
            entries: Iterator[object] = iter(engine.list_preset_voices())
            voices = []
            for entry in entries:
                label, voice_id = entry  # type: ignore[misc]
                voices.append(
                    engine_pb2.PresetVoice(id=str(voice_id), label=str(label), capabilities=["preset"])
                )
            return tuple(voices)
        except Exception as error:
            if isinstance(error, RuntimeFailure):
                raise
            raise RuntimeFailure("voice_list_failed", "VieNeu preset voices could not be listed") from error

    def engine_for(self, model_id: str) -> Any:
        self._require_model(model_id)
        assert self._engine is not None
        return self._engine

    def reference_cloning_supported(self) -> bool:
        if self._engine is None:
            return False
        target = getattr(self._engine, "infer_stream", None)
        return target is not None and _supports_reference_signature(
            target, allow_verified_kwargs=True
        )

    def align(self, model_id: str, audio_path: str, transcript: str) -> None:
        """Reject alignment until a real VieNeu aligner is installed."""
        del model_id, audio_path, transcript
        raise RuntimeFailure("alignment_unavailable", "VieNeu alignment is unavailable")

    def validate_voice(self, model_id: str, voice_id: str) -> None:
        self.engine_for(model_id)
        if not voice_id:
            raise RuntimeFailure("voice_not_found", "The requested preset voice was not found")
        if voice_id not in {voice.id for voice in self.list_voices(model_id)}:
            raise RuntimeFailure("voice_not_found", "The requested preset voice was not found")

    def validate_reference(
        self, model_id: str, reference_path: str, transcript: str | None
    ) -> engine_pb2.ReferenceMetadata:
        self.engine_for(model_id)
        if not self.reference_cloning_supported():
            raise RuntimeFailure("reference_unsupported", "Reference cloning is unavailable")
        try:
            return self._reference_validator.validate(model_id, reference_path, transcript)
        except ReferenceValidationError as error:
            raise RuntimeFailure(error.code, error.message) from error

    def synthesize(
        self,
        model_id: str,
        voice_id: str | None,
        text: str,
        cancellation: object,
        *,
        reference_path: str | None = None,
        transcript: str | None = None,
    ) -> Iterator[bytes]:
        """Yield validated PCM chunks while serializing access to the loaded SDK."""
        if self._synthesis_stuck:
            raise RuntimeFailure("synthesis_busy", "The VieNeu runtime is quarantined after stuck synthesis")
        if not self._synthesis_lock.acquire(blocking=False):
            raise RuntimeFailure("synthesis_busy", "The VieNeu runtime is already synthesizing")
        try:
            engine = self.engine_for(model_id)
            reference = reference_path is not None
            if reference:
                try:
                    if not self.reference_cloning_supported():
                        raise RuntimeFailure("reference_unsupported", "Reference cloning is unavailable")
                except ReferenceValidationError as error:
                    raise RuntimeFailure(error.code, error.message) from error
            else:
                self.validate_voice(model_id, voice_id or "")
            try:
                if reference:
                    with self._reference_validator.open_reference(reference_path or "") as descriptor:
                        self._reference_validator.validate_descriptor(
                            descriptor, reference_path or "", transcript
                        )
                        with tempfile.TemporaryDirectory(prefix="tts-studio-reference-") as directory:
                            snapshot = Path(directory) / ("reference" + Path(reference_path or "").suffix.lower())
                            with os.fdopen(os.dup(descriptor), "rb") as source, snapshot.open("wb") as destination:
                                shutil.copyfileobj(source, destination)
                                destination.flush()
                                os.fsync(destination.fileno())
                            stream = engine.infer_stream(
                                text, ref_audio=str(snapshot), ref_text=transcript
                            )
                            for samples in stream:
                                if _is_cancelled(cancellation):
                                    return
                                try:
                                    pcm = waveform_to_pcm16(samples)
                                except (TypeError, ValueError, OverflowError) as error:
                                    raise RuntimeFailure("invalid_audio", "VieNeu returned invalid audio") from error
                                if pcm:
                                    yield pcm
                else:
                    stream = engine.infer_stream(text, voice=voice_id)
                    for samples in stream:
                        if _is_cancelled(cancellation):
                            return
                        try:
                            pcm = waveform_to_pcm16(samples)
                        except (TypeError, ValueError, OverflowError) as error:
                            raise RuntimeFailure("invalid_audio", "VieNeu returned invalid audio") from error
                        if pcm:
                            yield pcm
            except RuntimeFailure:
                raise
            except Exception as error:
                raise RuntimeFailure("synthesis_failed", "VieNeu synthesis failed") from error
        finally:
            self._synthesis_lock.release()

    def _require_model(self, model_id: str) -> None:
        if self._engine is None or self._model_id != model_id:
            raise RuntimeFailure("model_not_loaded", "The requested model is not loaded")

    def _safe_cache_path(self, cache_path: str) -> Path:
        relative = PurePosixPath(cache_path)
        if (
            not cache_path
            or "\\" in cache_path
            or relative.is_absolute()
            or PureWindowsPath(cache_path).drive
            or PureWindowsPath(cache_path).is_absolute()
            or ".." in relative.parts
        ):
            raise RuntimeFailure("invalid_request", "The model cache path is invalid")
        cache = self._data_root.joinpath(*relative.parts)
        self._assert_no_symlink_components(cache)
        if not cache.is_dir():
            raise RuntimeFailure("model_load_failed", "The VieNeu model files are unavailable")
        return cache

    def _require_files(self, directory: Path, names: tuple[str, ...]) -> None:
        self._assert_no_symlink_components(directory)
        if not directory.is_dir():
            raise RuntimeFailure("model_load_failed", "The VieNeu model files are unavailable")
        for name in names:
            path = directory / name
            self._assert_no_symlink_components(path)
            if path.is_symlink() or not path.is_file():
                raise RuntimeFailure("model_load_failed", "The VieNeu model files are unavailable")

    def _assert_no_symlink_components(self, path: Path) -> None:
        try:
            relative = path.absolute().relative_to(self._data_root)
        except ValueError as error:
            raise RuntimeFailure("invalid_request", "The model cache path is invalid") from error
        current = self._data_root
        if current.is_symlink():
            raise RuntimeFailure("invalid_request", "The model cache path is invalid")
        for component in relative.parts:
            current /= component
            if current.is_symlink():
                raise RuntimeFailure("invalid_request", "The model cache path is invalid")


def _is_cancelled(cancellation: object) -> bool:
    checker = getattr(cancellation, "is_set", None)
    if callable(checker):
        return bool(checker())
    return bool(cancellation() if callable(cancellation) else cancellation)
