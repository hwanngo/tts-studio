import wave
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from tts_studio_vieneu_worker.reference import ReferenceValidationError, VieNeuReferenceValidator


def write_wav(path: Path, *, seconds: float = 1.0, channels: int = 1, value: float = 0.25) -> None:
    frames = int(16000 * seconds)
    samples = np.full((frames, channels), value, dtype=np.float32)
    pcm = np.rint(np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(pcm)


def test_validate_returns_metadata_for_valid_wav(tmp_path: Path) -> None:
    root = tmp_path
    path = root / "staging" / "references" / "ref.wav"
    path.parent.mkdir(parents=True)
    write_wav(path, channels=2)

    metadata = VieNeuReferenceValidator(root).validate("model-1", "staging/references/ref.wav", "hello")

    assert metadata.sample_rate_hz == 16000
    assert metadata.channels == 2
    assert metadata.duration_ms == 1000
    assert metadata.byte_size == path.stat().st_size
    assert metadata.container == "wav"


def test_validate_returns_metadata_for_valid_flac(tmp_path: Path) -> None:
    path = tmp_path / "staging" / "references" / "ref.flac"
    path.parent.mkdir(parents=True)
    sf.write(path, np.zeros((1600, 1), dtype=np.float32), 16000, format="FLAC")

    metadata = VieNeuReferenceValidator(tmp_path).validate("model-1", "staging/references/ref.flac", None)

    assert metadata.sample_rate_hz == 16000
    assert metadata.channels == 1
    assert metadata.duration_ms == 100
    assert metadata.container == "flac"


@pytest.mark.parametrize(
    ("samples", "channels"),
    [(np.array([], dtype=np.float32), 1), (np.array([[np.nan]], dtype=np.float32), 1),
     (np.array([[np.inf]], dtype=np.float32), 1), (np.zeros((1600, 3), dtype=np.float32), 3)],
)
def test_validate_rejects_non_finite_empty_or_unsupported_audio(
    tmp_path: Path, samples: np.ndarray, channels: int
) -> None:
    path = tmp_path / "staging" / "references" / "ref.wav"
    path.parent.mkdir(parents=True)
    sf.write(path, samples, 16000, format="WAV", subtype="FLOAT")

    with pytest.raises(ReferenceValidationError) as error:
        VieNeuReferenceValidator(tmp_path).validate("model-1", "staging/references/ref.wav", None)

    assert error.value.code == "reference_invalid"


@pytest.mark.parametrize(
    "reference_path",
    ["../outside.wav", "staging\\references\\ref.wav", "/tmp/ref.wav", "staging/references/missing.wav"],
)
def test_validate_rejects_unsafe_or_missing_paths(tmp_path: Path, reference_path: str) -> None:
    with pytest.raises(ReferenceValidationError) as error:
        VieNeuReferenceValidator(tmp_path).validate("model-1", reference_path, None)

    assert error.value.code == "reference_invalid"


def test_validate_rejects_symlink_component(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    write_wav(outside / "ref.wav")
    (tmp_path / "staging").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ReferenceValidationError) as error:
        VieNeuReferenceValidator(tmp_path).validate("model-1", "staging/ref.wav", None)

    assert error.value.code == "reference_invalid"


def test_validate_keeps_original_directory_handle_when_component_is_swapped(
    tmp_path: Path, monkeypatch
) -> None:
    references = tmp_path / "staging" / "references"
    references.mkdir(parents=True)
    write_wav(references / "ref.wav", seconds=1.0)
    outside = tmp_path / "outside"
    outside.mkdir()
    write_wav(outside / "ref.wav", seconds=2.0)
    real_open = __import__("os").open
    swapped = False

    def swap_before_final_open(path, *args, **kwargs):
        nonlocal swapped
        if not swapped and __import__("os").path.basename(__import__("os").fspath(path)) == "ref.wav":
            swapped = True
            references.rename(tmp_path / "references-original")
            references.symlink_to(outside, target_is_directory=True)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("os.open", swap_before_final_open)
    metadata = VieNeuReferenceValidator(tmp_path).validate(
        "model-1", "staging/references/ref.wav", None
    )

    assert metadata.duration_ms == 1000


@pytest.mark.parametrize("transcript", ["bad\x00text", "x" * 2001])
def test_validate_rejects_unsafe_transcript(tmp_path: Path, transcript: str) -> None:
    path = tmp_path / "staging" / "references" / "ref.wav"
    path.parent.mkdir(parents=True)
    write_wav(path)

    with pytest.raises(ReferenceValidationError) as error:
        VieNeuReferenceValidator(tmp_path).validate("model-1", "staging/references/ref.wav", transcript)

    assert error.value.code == "reference_invalid"


def test_validate_rejects_compressed_reference_over_budget_before_decode(tmp_path: Path) -> None:
    path = tmp_path / "staging" / "references" / "huge.wav"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"RIFF" + b"x" * (20 * 1024 * 1024))

    with pytest.raises(ReferenceValidationError) as error:
        VieNeuReferenceValidator(tmp_path).validate("model-1", "staging/references/huge.wav", None)

    assert error.value.code == "reference_too_large"


def test_validate_rejects_duration_over_eight_seconds(tmp_path: Path) -> None:
    path = tmp_path / "staging" / "references" / "long.wav"
    path.parent.mkdir(parents=True)
    write_wav(path, seconds=8.01)

    with pytest.raises(ReferenceValidationError) as error:
        VieNeuReferenceValidator(tmp_path).validate("model-1", "staging/references/long.wav", None)

    assert error.value.code == "reference_invalid"
