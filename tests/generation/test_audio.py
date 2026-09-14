import asyncio
from types import SimpleNamespace

import pytest

from tts_studio.generation.audio import (
    AudioFormat,
    AudioValidationError,
    ByteBudgetQueue,
    PcmValidator,
)


def test_validator_accepts_valid_header_chunks_and_result() -> None:
    validator = PcmValidator()

    validator.accept_header(AudioFormat(sample_rate=48_000, channels=1, sample_format="S16LE"))
    validator.accept_chunk(0, b"\x01\x00\x02\x00")
    validator.accept_chunk(1, b"\x03\x00")

    result = validator.finish(total_frames=3)

    assert result.format == AudioFormat(sample_rate=48_000, channels=1, sample_format="S16LE")
    assert result.byte_count == 6
    assert result.frame_count == 3


@pytest.mark.parametrize(
    "header",
    [
        AudioFormat(sample_rate=44_100, channels=1, sample_format="S16LE"),
        AudioFormat(sample_rate=48_000, channels=2, sample_format="S16LE"),
        AudioFormat(sample_rate=48_000, channels=1, sample_format="F32LE"),
    ],
)
def test_validator_rejects_unsupported_headers(header: AudioFormat) -> None:
    with pytest.raises(AudioValidationError):
        PcmValidator().accept_header(header)


@pytest.mark.parametrize(
    "header",
    [
        AudioFormat(sample_rate=48_000.0, channels=1, sample_format="S16LE"),
        AudioFormat(sample_rate=48_000, channels=1.0, sample_format="S16LE"),
        AudioFormat(sample_rate=48_000, channels=1, sample_format="S16LE", sample_width=2.0),
        SimpleNamespace(sample_rate_hz=48_000.0, channels=1, sample_format=1),
        SimpleNamespace(sample_rate_hz=48_000, channels=1.0, sample_format=1),
    ],
)
def test_validator_rejects_numerically_equal_non_integer_format_fields(header: object) -> None:
    with pytest.raises(AudioValidationError):
        PcmValidator().accept_header(header)


def test_validator_requires_header_before_chunk_and_rejects_duplicate_header() -> None:
    validator = PcmValidator()

    with pytest.raises(AudioValidationError):
        validator.accept_chunk(0, b"\x00\x00")

    validator.accept_header(AudioFormat(48_000, 1, "S16LE"))
    with pytest.raises(AudioValidationError):
        validator.accept_header(AudioFormat(48_000, 1, "S16LE"))


@pytest.mark.parametrize("sequences", [(1,), (0, 2), (0, 0)])
def test_validator_rejects_sequence_gaps_duplicates_and_nonzero_start(
    sequences: tuple[int, ...],
) -> None:
    validator = PcmValidator()
    validator.accept_header(AudioFormat(48_000, 1, "S16LE"))

    with pytest.raises(AudioValidationError):
        for sequence in sequences:
            validator.accept_chunk(sequence, b"\x00\x00")


def test_validator_rejects_misaligned_or_oversized_pcm() -> None:
    validator = PcmValidator(max_bytes=4)
    validator.accept_header(AudioFormat(48_000, 1, "S16LE"))

    with pytest.raises(AudioValidationError):
        validator.accept_chunk(0, b"\x00")

    validator.accept_chunk(0, b"\x00\x00\x00\x00")
    with pytest.raises(AudioValidationError):
        validator.accept_chunk(1, b"\x00\x00")


def test_validator_rejects_declared_frame_mismatch() -> None:
    validator = PcmValidator()
    validator.accept_header(AudioFormat(48_000, 1, "S16LE"))
    validator.accept_chunk(0, b"\x00\x00\x00\x00")

    with pytest.raises(AudioValidationError):
        validator.finish(total_frames=1)


@pytest.mark.asyncio
async def test_byte_budget_queue_applies_a_one_mib_byte_budget() -> None:
    queue = ByteBudgetQueue()
    payload = b"x" * (1024 * 1024)

    await queue.put(payload)
    assert queue.pending_bytes == len(payload)
    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait(b"x")

    assert await queue.get() == payload
    assert queue.pending_bytes == 0


@pytest.mark.asyncio
async def test_byte_budget_queue_rejects_one_item_larger_than_budget() -> None:
    queue = ByteBudgetQueue(max_bytes=3)

    with pytest.raises(asyncio.QueueFull):
        queue.put_nowait(b"1234")
