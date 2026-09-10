"""Strict conversion from SDK waveforms to protocol PCM."""

import numpy as np


def waveform_to_pcm16(samples: object) -> bytes:
    """Convert one finite float waveform to deterministic mono S16LE PCM."""
    array = np.asarray(samples)
    if array.ndim != 1 or array.dtype.kind != "f":
        raise ValueError("waveform must be a one-dimensional floating-point array")
    if not np.isfinite(array).all():
        raise ValueError("waveform must contain only finite values")
    if array.size == 0:
        return b""
    clipped = np.clip(array.astype(np.float32, copy=False), -1.0, 1.0)
    pcm = np.rint(clipped * 32767.0).astype("<i2", copy=False)
    return pcm.tobytes()
