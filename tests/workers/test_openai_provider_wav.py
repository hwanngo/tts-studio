import io
import sys
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "workers" / "openai_compatible" / "src"))

from tts_studio_openai_worker.wav import ProviderAudioError, validate_wav


def _wav(*, rate: int = 48000, channels: int = 1, width: int = 2) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setframerate(rate)
        writer.setnchannels(channels)
        writer.setsampwidth(width)
        writer.writeframes(b"\0" * width * channels * 4)
    return output.getvalue()


def test_validate_wav_returns_core_pcm_metadata() -> None:
    rate, frames, pcm = validate_wav(_wav())
    assert (rate, frames, len(pcm)) == (48000, 4, 8)


@pytest.mark.parametrize("kwargs", [{"rate": 16000}, {"channels": 2}, {"width": 1}])
def test_validate_wav_rejects_non_core_audio(kwargs: dict[str, int]) -> None:
    with pytest.raises(ProviderAudioError):
        validate_wav(_wav(**kwargs))
