import asyncio
import os
import struct
import wave
from collections.abc import Callable
from pathlib import Path

import pytest

import tts_studio.generation.wav as wav_module
from tts_studio.generation.audio import AudioFormat, AudioValidationError, PcmValidator
from tts_studio.generation.wav import WavArtifactWriter
from tts_studio.storage.layout import StorageLayout, UnsafeStoragePathError


def _layout(tmp_path: Path) -> StorageLayout:
    layout = StorageLayout.from_root(tmp_path / ".tts-studio")
    layout.ensure()
    return layout


def test_writer_finalizes_exact_pcm_as_valid_managed_wav(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    writer = WavArtifactWriter(layout, "artifact-one")
    pcm = b"\x01\x00\x02\x00\xff\xff"

    writer.write(pcm)
    artifact = writer.finalize()

    assert artifact == layout.audio / "artifact-one.wav"
    assert artifact.read_bytes()[0:4] == b"RIFF"
    with wave.open(str(artifact), "rb") as stream:
        assert stream.getnchannels() == 1
        assert stream.getsampwidth() == 2
        assert stream.getframerate() == 48_000
        assert stream.getnframes() == 3
        assert stream.readframes(3) == pcm
    contents = artifact.read_bytes()
    riff_size = struct.unpack_from("<I", contents, 4)[0]
    data_size = struct.unpack_from("<I", contents, 40)[0]
    assert riff_size == 36 + len(pcm)
    assert data_size == len(pcm)
    assert not writer.temporary_path.exists()


def test_writer_abort_removes_private_file_and_never_publishes(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    writer = WavArtifactWriter(layout, "artifact-two")
    writer.write(b"\x00\x00")
    temporary = writer.temporary_path

    writer.abort()

    assert not temporary.exists()
    assert not (layout.audio / "artifact-two.wav").exists()


@pytest.mark.parametrize("mutation", ["replace", "truncate", "same-size"])
def test_writer_rejects_substituted_or_modified_pcm(tmp_path: Path, mutation: str) -> None:
    layout = _layout(tmp_path)
    writer = WavArtifactWriter(layout, "tampered")
    writer.write(b"\x01\x00" * 4)
    if mutation == "replace":
        writer.temporary_path.unlink()
    writer.temporary_path.write_bytes(b"\xff\x00" * (2 if mutation == "truncate" else 4))

    with pytest.raises((UnsafeStoragePathError, ValueError)):
        writer.finalize()

    assert not (layout.audio / "tampered.wav").exists()
    assert not writer.temporary_path.exists()


def test_writer_does_not_reopen_substituted_wav_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    writer = WavArtifactWriter(layout, "wav-substitution")
    writer.write(b"\x01\x00" * 4)
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"untouched")
    original_open = wave.open

    def substitute_before_wave_open(file, mode=None):
        staged = next(layout.audio.glob(".wav-substitution.*.wav"))
        staged.unlink()
        staged.symlink_to(outside)
        return original_open(file, mode)

    monkeypatch.setattr(wav_module.wave, "open", substitute_before_wave_open)
    with pytest.raises(UnsafeStoragePathError):
        writer.finalize()
    assert outside.read_bytes() == b"untouched"
    assert not (layout.audio / "wav-substitution.wav").exists()


def test_discard_reports_replaced_publication_without_deleting_replacement(tmp_path: Path) -> None:
    writer = WavArtifactWriter(_layout(tmp_path), "replaced-publication")
    writer.write(b"\x01\x00")
    destination = writer.finalize()
    replacement = tmp_path / "replacement.wav"
    replacement.write_bytes(b"replacement")
    os.replace(replacement, destination)
    with pytest.raises(UnsafeStoragePathError):
        writer.discard_published()
    assert destination.read_bytes() == b"replacement"


@pytest.mark.parametrize(
    "totals", [{"byte_count": 6, "frame_count": 4}, {"byte_count": 8, "frame_count": 3}]
)
def test_finalization_rejects_totals_different_from_validated_pcm(
    tmp_path: Path, totals: dict[str, int]
) -> None:
    layout = _layout(tmp_path)
    writer = WavArtifactWriter(layout, "wrong-totals")
    writer.write(b"\x00\x00" * 4)
    with pytest.raises(ValueError, match="total"):
        writer.finalize(**totals)
    assert tuple(layout.staging.iterdir()) == ()
    assert tuple(layout.audio.iterdir()) == ()


def test_writer_cleans_up_after_finalize_failure(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    writer = WavArtifactWriter(layout, "artifact-three")
    writer.write(b"\x00\x00")
    destination = layout.audio / "artifact-three.wav"
    destination.symlink_to(tmp_path / "outside.wav")

    with pytest.raises(UnsafeStoragePathError):
        writer.finalize()

    assert not writer.temporary_path.exists()
    assert destination.is_symlink()


def _run_stream_with_cleanup(writer: WavArtifactWriter, operation: Callable[[], object]) -> None:
    try:
        operation()
    except BaseException:
        writer.abort()
        raise


def test_malformed_stream_cleanup_removes_staged_pcm(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    writer = WavArtifactWriter(layout, "malformed-stream")
    validator = PcmValidator()
    writer.write(b"\x00\x00")

    with pytest.raises(AudioValidationError):
        _run_stream_with_cleanup(
            writer,
            lambda: (
                validator.accept_header(AudioFormat(48_000, 1, "S16LE")),
                validator.accept_chunk(0, b"\x00"),
            ),
        )

    assert not writer.temporary_path.exists()
    assert not (layout.audio / "malformed-stream.wav").exists()


@pytest.mark.parametrize("failure", [asyncio.CancelledError(), RuntimeError("worker failed")])
def test_cancellation_or_worker_failure_cleanup_removes_staged_pcm(
    tmp_path: Path, failure: BaseException
) -> None:
    layout = _layout(tmp_path)
    writer = WavArtifactWriter(layout, "failed-stream")
    writer.write(b"\x00\x00")

    with pytest.raises(type(failure)):
        _run_stream_with_cleanup(writer, lambda: (_ for _ in ()).throw(failure))

    assert not writer.temporary_path.exists()
    assert not (layout.audio / "failed-stream.wav").exists()


def test_post_publication_failure_removes_published_artifact_and_temp_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    writer = WavArtifactWriter(layout, "post-publication-failure")
    writer.write(b"\x00\x00")
    destination = layout.audio / "post-publication-failure.wav"

    def fail_directory_fsync(_: Path) -> None:
        raise OSError("simulated directory fsync failure")

    monkeypatch.setattr(wav_module, "_fsync_directory", fail_directory_fsync)
    with pytest.raises(OSError, match="directory fsync"):
        writer.finalize()

    assert not destination.exists()
    assert not writer.temporary_path.exists()
    assert not list(layout.audio.glob(".post-publication-failure.*.wav"))


def test_identity_is_captured_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    writer = WavArtifactWriter(layout, "identity-capture-failure")
    writer.write(b"\x00\x00")
    destination = layout.audio / "identity-capture-failure.wav"

    def fail_post_publication_identity(path: Path) -> tuple[int, int]:
        if path == destination:
            raise OSError("simulated identity capture failure")
        metadata = path.stat()
        return metadata.st_dev, metadata.st_ino

    monkeypatch.setattr(wav_module, "_file_identity", fail_post_publication_identity)
    assert writer.finalize() == destination

    assert destination.exists()
    assert not writer.temporary_path.exists()
    assert not list(layout.audio.glob(".identity-capture-failure.*.wav"))


def test_cleanup_quarantines_before_deleting_and_preserves_replacement_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    destination = layout.audio / "cleanup-race.wav"
    destination.write_bytes(b"published")
    identity = wav_module._file_identity(destination)
    replacement = tmp_path / "cleanup-replacement.wav"
    replacement_bytes = b"replacement"
    original_replace = os.replace

    def replace_with_race(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        if Path(source) == destination:
            replacement.write_bytes(replacement_bytes)
            original_replace(replacement, destination)
        original_replace(source, target)

    monkeypatch.setattr(wav_module.os, "replace", replace_with_race)
    with pytest.raises(UnsafeStoragePathError):
        wav_module._remove_published_file(destination, identity)

    assert destination.read_bytes() == replacement_bytes
    assert not list(layout.audio.glob(".cleanup-*.wav"))
