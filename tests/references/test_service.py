import hashlib
import os
from pathlib import Path

import pytest

from tts_studio.references.domain import (
    CleanupFailedError,
    ReferenceMetadata,
    ReferenceRecoveryRequiredError,
    ReferenceState,
    UploadTooLargeError,
)
from tts_studio.references.service import MAX_REFERENCE_BYTES, ReferenceService
from tts_studio.storage.db import Database
from tts_studio.storage.layout import StorageLayout


def _service(tmp_path: Path) -> tuple[StorageLayout, ReferenceService]:
    layout = StorageLayout.from_root(tmp_path / ".tts-studio")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    return layout, ReferenceService(database, layout)


def test_upload_is_core_owned_hashed_exclusive_and_keeps_transcript_in_memory(
    tmp_path: Path,
) -> None:
    layout, service = _service(tmp_path)
    payload = b"reference-audio"
    recording = service.create_upload(
        model_id="model-1", payload=payload, transcript="hello", now="2026-09-07T00:00:00+00:00"
    )

    path = layout.root / recording.relative_path
    assert path.read_bytes() == payload
    assert recording.byte_size == len(payload)
    assert recording.sha256 == hashlib.sha256(payload).hexdigest()
    assert recording.relative_path == f"staging/references/{recording.id}"
    with service._database.read() as connection:
        row = connection.execute(
            "SELECT * FROM reference_recordings WHERE id = ?", (recording.id,)
        ).fetchone()
    assert row["transcript_present"] == 1
    assert "hello" not in repr(tuple(row))


def test_upload_rejects_more_than_20_mib_and_leaves_no_row_or_file(tmp_path: Path) -> None:
    layout, service = _service(tmp_path)
    with pytest.raises(UploadTooLargeError):
        service.create_upload(model_id="model", payload=b"x" * (MAX_REFERENCE_BYTES + 1))
    assert tuple(layout.reference_staging.iterdir()) == ()
    with service._database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM reference_recordings").fetchone()[0] == 0


def test_upload_fails_closed_when_reference_staging_is_replaced_after_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout, service = _service(tmp_path)
    references = layout.reference_staging
    original_open = os.open
    replaced = False

    def replace_before_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal replaced
        if path == references and not replaced:
            replaced = True
            target = tmp_path / "replacement"
            target.mkdir()
            references.rename(tmp_path / "original-references")
            references.symlink_to(target, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_before_open)
    with pytest.raises((OSError, RuntimeError)):
        service.create_upload(model_id="model", payload=b"audio")
    replacement = tmp_path / "replacement"
    assert not replacement.exists() or tuple(replacement.iterdir()) == ()


def test_claim_is_one_time_and_release_removes_file_and_metadata(tmp_path: Path) -> None:
    layout, service = _service(tmp_path)
    recording = service.create_upload(model_id="model", payload=b"wav", transcript="words")
    service.mark_validated(recording.id, ReferenceMetadata("wav", 16000, 1, 100))
    handle = service.claim_for_generation(recording.id)
    assert handle.transcript == "words"
    assert handle.recording.state is ReferenceState.CONSUMED
    with pytest.raises(LookupError):
        service.claim_for_generation(recording.id)
    service.release_terminal(handle)
    assert not (layout.root / recording.relative_path).exists()
    with pytest.raises(LookupError):
        service.get(recording.id)
    service.release_terminal(handle)


def test_cleanup_does_not_follow_redirected_reference_path(tmp_path: Path) -> None:
    layout, service = _service(tmp_path)
    recording = service.create_upload(model_id="model", payload=b"wav")
    path = layout.root / recording.relative_path
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    path.unlink()
    path.symlink_to(outside)

    with pytest.raises(CleanupFailedError):
        service.delete(recording.id)
    assert outside.read_bytes() == b"keep"
    assert service.get(recording.id).state is ReferenceState.CLEANUP_FAILED


def test_cleanup_records_failure_when_reference_staging_is_redirected(tmp_path: Path) -> None:
    layout, service = _service(tmp_path)
    recording = service.create_upload(model_id="model", payload=b"wav")
    staging = layout.reference_staging
    redirected_target = tmp_path / "redirected-target"
    redirected_target.mkdir()
    staging.rename(tmp_path / "original-staging")
    staging.symlink_to(redirected_target, target_is_directory=True)

    with pytest.raises(CleanupFailedError):
        service.delete(recording.id)

    assert service.get(recording.id).state is ReferenceState.CLEANUP_FAILED
    assert tuple(redirected_target.iterdir()) == ()


def test_restart_without_transcript_fails_closed_and_cleans(tmp_path: Path) -> None:
    layout, service = _service(tmp_path)
    recording = service.create_upload(model_id="model", payload=b"wav", transcript="words")
    service.mark_validated(recording.id, ReferenceMetadata("wav", 16000, 1, 100))
    restarted = ReferenceService(service._database, layout)

    with pytest.raises(ReferenceRecoveryRequiredError):
        restarted.claim_for_generation(recording.id)
    assert not (layout.root / recording.relative_path).exists()
    with pytest.raises(LookupError):
        restarted.get(recording.id)


def test_recover_removes_orphan_and_terminal_reference_files(tmp_path: Path) -> None:
    layout, service = _service(tmp_path)
    orphan = layout.reference_staging / "orphan"
    orphan.write_bytes(b"orphan")
    recording = service.create_upload(model_id="model", payload=b"wav")
    service.mark_validated(recording.id, ReferenceMetadata("wav", 16000, 1, 100))
    service.claim_for_generation(recording.id)
    recovered = service.recover()
    assert recovered >= 1
    assert not orphan.exists()
    assert not (layout.root / recording.relative_path).exists()


def test_recover_expires_and_cleans_due_unconsumed_references(tmp_path: Path) -> None:
    layout, service = _service(tmp_path)
    created = service.create_upload(
        model_id="model", payload=b"uploaded", now="2026-09-07T00:00:00+00:00"
    )
    validated = service.create_upload(
        model_id="model", payload=b"validated", now="2026-09-07T00:00:00+00:00"
    )
    service.mark_validated(
        validated.id,
        ReferenceMetadata("wav", 16000, 1, 100),
        now="2026-09-07T00:01:00+00:00",
    )

    recovered = service.recover(now="2026-09-07T01:00:00+00:00")

    assert recovered == 2
    for recording in (created, validated):
        assert not (layout.root / recording.relative_path).exists()
        with pytest.raises(LookupError):
            service.get(recording.id)


def test_recover_retries_expired_cleanup_after_safe_staging_is_restored(tmp_path: Path) -> None:
    layout, service = _service(tmp_path)
    recording = service.create_upload(
        model_id="model", payload=b"wav", now="2026-09-07T00:00:00+00:00"
    )
    path = layout.root / recording.relative_path
    redirected_target = tmp_path / "redirected-target"
    redirected_target.write_bytes(b"outside")
    path.unlink()
    path.symlink_to(redirected_target)

    assert service.recover(now="2026-09-07T01:00:00+00:00") == 0
    assert service.get(recording.id).state is ReferenceState.CLEANUP_FAILED
    assert redirected_target.read_bytes() == b"outside"

    path.unlink()
    path.write_bytes(b"wav")
    assert service.recover(now="2026-09-07T01:01:00+00:00") == 1
    with pytest.raises(LookupError):
        service.get(recording.id)
