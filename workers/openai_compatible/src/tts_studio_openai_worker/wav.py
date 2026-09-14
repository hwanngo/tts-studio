from __future__ import annotations

import io
import wave


class ProviderAudioError(ValueError):
    pass


def validate_wav(payload: bytes, *, max_bytes: int = 20 * 1024 * 1024) -> tuple[int, int, bytes]:
    if len(payload) > max_bytes:
        raise ProviderAudioError("provider audio exceeds the response size limit")
    try:
        with wave.open(io.BytesIO(payload), "rb") as source:
            if (
                source.getnchannels() != 1
                or source.getsampwidth() != 2
                or source.getframerate() != 48000
            ):
                raise ProviderAudioError("provider audio format is unsupported")
            frames = source.getnframes()
            pcm = source.readframes(frames)
    except (wave.Error, EOFError, OSError) as error:
        raise ProviderAudioError("provider audio is not a valid WAV file") from error
    if len(pcm) != frames * 2:
        raise ProviderAudioError("provider audio payload is truncated")
    return 48000, frames, pcm
