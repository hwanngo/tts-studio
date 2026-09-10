"""Validation and bounded transport helpers for Worker PCM streams."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any


class AudioValidationError(ValueError):
    """Raised when a Worker audio stream violates the Core audio contract."""


class ByteBudgetQueueSignal(Enum):
    """Explicit non-audio messages carried by a byte-budgeted PCM queue."""

    END = "end"


@dataclass(frozen=True)
class AudioFormat:
    sample_rate: int
    channels: int
    sample_format: str = "S16LE"
    sample_width: int = 2

    @property
    def frame_width(self) -> int:
        return self.channels * self.sample_width


@dataclass(frozen=True)
class PcmResult:
    format: AudioFormat
    byte_count: int
    frame_count: int


class PcmValidator:
    """Validate one ordered, bounded PCM stream before WAV finalization."""

    def __init__(self, *, max_bytes: int = 256 * 1024 * 1024) -> None:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
            raise ValueError("max_bytes must be a non-negative integer")
        self._max_bytes = max_bytes
        self._format: AudioFormat | None = None
        self._next_sequence = 0
        self._byte_count = 0
        self._finished = False

    @property
    def audio_format(self) -> AudioFormat | None:
        return self._format

    @property
    def byte_count(self) -> int:
        return self._byte_count

    def accept_header(self, header: AudioFormat | Any) -> AudioFormat:
        if self._finished:
            raise AudioValidationError("audio stream is already finished")
        if self._format is not None:
            raise AudioValidationError("audio header must be sent exactly once")
        audio_format = _coerce_format(header)
        if audio_format != AudioFormat(48_000, 1, "S16LE", 2):
            raise AudioValidationError("only 48 kHz mono S16LE audio is supported")
        self._format = audio_format
        return audio_format

    def accept_chunk(self, sequence: int | Any, pcm: bytes | bytearray | memoryview | None = None) -> None:
        if self._finished:
            raise AudioValidationError("audio stream is already finished")
        if self._format is None:
            raise AudioValidationError("audio header must precede PCM chunks")
        if pcm is None and not isinstance(sequence, int):
            chunk = sequence
            sequence = getattr(chunk, "sequence", None)
            pcm = getattr(chunk, "pcm", None)
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise AudioValidationError("PCM sequence must be an integer")
        if sequence != self._next_sequence:
            raise AudioValidationError(
                f"PCM sequence must be {self._next_sequence}, got {sequence}"
            )
        if not isinstance(pcm, (bytes, bytearray, memoryview)):
            raise AudioValidationError("PCM payload must be bytes")
        payload = bytes(pcm)
        if len(payload) % self._format.frame_width:
            raise AudioValidationError("PCM chunk is not aligned to one audio frame")
        if self._byte_count + len(payload) > self._max_bytes:
            raise AudioValidationError("PCM stream exceeds the configured byte limit")
        self._byte_count += len(payload)
        self._next_sequence += 1

    def finish(self, total_frames: int | Any) -> PcmResult:
        if self._finished:
            raise AudioValidationError("audio stream is already finished")
        if self._format is None:
            raise AudioValidationError("audio stream has no header")
        if not isinstance(total_frames, int):
            total_frames = getattr(total_frames, "total_frames", None)
        if not isinstance(total_frames, int) or isinstance(total_frames, bool) or total_frames < 0:
            raise AudioValidationError("total_frames must be a non-negative integer")
        actual_frames, remainder = divmod(self._byte_count, self._format.frame_width)
        if remainder or total_frames != actual_frames:
            raise AudioValidationError(
                f"declared frame total {total_frames} does not match {actual_frames} PCM frames"
            )
        self._finished = True
        return PcmResult(self._format, self._byte_count, actual_frames)


class ByteBudgetQueue:
    """A single-consumer queue bounded by total payload bytes rather than item count."""

    def __init__(self, max_bytes: int = 1024 * 1024) -> None:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self.max_bytes = max_bytes
        self._items: deque[bytes | ByteBudgetQueueSignal] = deque()
        self._pending_bytes = 0
        self._condition = asyncio.Condition()

    @property
    def pending_bytes(self) -> int:
        return self._pending_bytes

    def full(self) -> bool:
        return self._pending_bytes >= self.max_bytes

    async def put(self, item: bytes | bytearray | memoryview | ByteBudgetQueueSignal) -> None:
        payload = _payload(item)
        if len(payload) > self.max_bytes:
            raise asyncio.QueueFull
        async with self._condition:
            while self._pending_bytes + len(payload) > self.max_bytes:
                await self._condition.wait()
            self._items.append(item if isinstance(item, ByteBudgetQueueSignal) else payload)
            self._pending_bytes += len(payload)
            self._condition.notify_all()

    def put_nowait(self, item: bytes | bytearray | memoryview | ByteBudgetQueueSignal) -> None:
        payload = _payload(item)
        if len(payload) > self.max_bytes or self._pending_bytes + len(payload) > self.max_bytes:
            raise asyncio.QueueFull
        self._items.append(item if isinstance(item, ByteBudgetQueueSignal) else payload)
        self._pending_bytes += len(payload)

    async def get(self) -> bytes | ByteBudgetQueueSignal:
        async with self._condition:
            while not self._items:
                await self._condition.wait()
            item = self._items.popleft()
            self._pending_bytes -= len(_payload(item))
            self._condition.notify_all()
            return item


def _payload(item: bytes | bytearray | memoryview | ByteBudgetQueueSignal) -> bytes:
    if isinstance(item, ByteBudgetQueueSignal):
        return b""
    if not isinstance(item, (bytes, bytearray, memoryview)):
        raise TypeError("queue items must be bytes")
    return bytes(item)


def _coerce_format(header: AudioFormat | Any) -> AudioFormat:
    try:
        if isinstance(header, AudioFormat):
            _require_exact_int(header.sample_rate, "sample_rate")
            _require_exact_int(header.channels, "channels")
            _require_exact_int(header.sample_width, "sample_width")
            return header
        sample_format = header.sample_format
        if type(sample_format) is int and sample_format == 1:
            sample_format = "S16LE"
        elif hasattr(sample_format, "name"):
            sample_format = sample_format.name
        sample_rate = _require_exact_int(header.sample_rate_hz, "sample_rate_hz")
        channels = _require_exact_int(header.channels, "channels")
        return AudioFormat(
            sample_rate=sample_rate,
            channels=channels,
            sample_format=str(sample_format),
            sample_width=2,
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise AudioValidationError("malformed audio header") from error


def _require_exact_int(value: Any, name: str) -> int:
    if type(value) is not int:
        raise AudioValidationError(f"{name} must be an exact integer")
    return value
