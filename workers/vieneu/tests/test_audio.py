import numpy as np
import pytest
from tts_studio_vieneu_worker.audio import waveform_to_pcm16


def test_waveform_to_pcm16_rounds_clips_and_uses_little_endian_s16() -> None:
    samples = np.array([-2.0, -1.0, -0.5, 0.5, 1.0, 2.0], dtype=np.float32)

    assert waveform_to_pcm16(samples) == b"\x01\x80\x01\x80\x00\xc0\x00@\xff\x7f\xff\x7f"


@pytest.mark.parametrize("samples", [np.array([np.nan]), np.array([np.inf]), np.array([[-1.0]])])
def test_waveform_to_pcm16_rejects_non_finite_and_non_1d_arrays(samples: object) -> None:
    with pytest.raises(ValueError):
        waveform_to_pcm16(samples)


def test_waveform_to_pcm16_skips_empty_arrays_and_preserves_alignment() -> None:
    assert waveform_to_pcm16(np.array([], dtype=np.float32)) == b""
    assert len(waveform_to_pcm16(np.array([0.1, 0.2], dtype=np.float32))) % 2 == 0
