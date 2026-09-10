import os
import sqlite3
from pathlib import Path

import pytest

from tts_studio.references.domain import ReferenceMetadata
from tts_studio.references.registry import ReferenceRecordNotFoundError
from tts_studio.references.service import ReferenceService
from tts_studio.storage.db import Database
from tts_studio.storage.layout import StorageLayout
from tts_studio.voices import service as voice_service
from tts_studio.voices.registry import SavedVoiceRegistry
from tts_studio.voices.service import SavedVoiceService


def _simulated_identity_unlink(
    directory_fd: int, name: str, file_fd: int, *, remove_directory: bool = False
) -> None:
    del file_fd
    if remove_directory:
        os.rmdir(name, dir_fd=directory_fd)
    else:
        os.unlink(name, dir_fd=directory_fd)


def test_saved_voice_registry_round_trip_and_model_filter(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite3")
    database.migrate()
    registry = SavedVoiceRegistry(database)
    created = registry.create(voice_id="voice-one", model_id="model-one", label="Alice", relative_path="voices/voice-one/reference.wav", transcript="hello")
    assert registry.get(created.id) == created
    assert registry.list_for_model("model-one") == (created,)
    assert registry.list_for_model("model-two") == ()


def test_saved_voice_registry_rejects_duplicate_ids(tmp_path: Path) -> None:
    database = Database(tmp_path / "db.sqlite3")
    database.migrate()
    registry = SavedVoiceRegistry(database)
    values = {
        "voice_id": "voice-one",
        "model_id": "model-one",
        "label": "Alice",
        "relative_path": "voices/voice-one/reference.wav",
        "transcript": None,
    }
    registry.create(**values)
    with pytest.raises(sqlite3.IntegrityError):
        registry.create(**values)


def test_saved_voice_service_copies_and_consumes_validated_reference(tmp_path: Path) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    references = ReferenceService(database, layout)
    reference = references.create_upload(model_id="model-one", payload=b"reference", transcript="hello")
    references.mark_validated(
        reference.id, ReferenceMetadata(container="wav", sample_rate_hz=16000, channels=1, duration_ms=1)
    )
    service = SavedVoiceService(SavedVoiceRegistry(database), references, layout)

    voice = service.create_from_reference(model_id="model-one", label="Alice", reference_id=reference.id)

    assert voice.transcript == "hello"
    assert (layout.root / Path(*voice.relative_path.split("/"))).read_bytes() == b"reference"
    with pytest.raises(ReferenceRecordNotFoundError):
        references.get(reference.id)


def test_saved_voice_copy_rejects_replaced_reference_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    references = ReferenceService(database, layout)
    reference = references.create_upload(model_id="model-one", payload=b"original")
    references.mark_validated(reference.id, ReferenceMetadata("wav", 16000, 1, 1))
    original_source = references.saved_voice_source

    def replace_after_validation(reference_id: str) -> tuple[object, Path, str | None]:
        recording, source, transcript = original_source(reference_id)
        replacement = source.with_name("replacement")
        source.unlink()
        replacement.write_bytes(b"replacement")
        replacement.rename(source)
        return recording, source, transcript

    monkeypatch.setattr(references, "saved_voice_source", replace_after_validation)
    service = SavedVoiceService(SavedVoiceRegistry(database), references, layout)

    with pytest.raises(RuntimeError):
        service.create_from_reference(model_id="model-one", label="Alice", reference_id=reference.id)
    assert service.list_for_model("model-one") == ()


def test_saved_voice_delete_keeps_metadata_when_file_is_replaced(tmp_path: Path) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    registry = SavedVoiceRegistry(database)
    directory = layout.managed_child("voices", "voice-one")
    directory.mkdir()
    path = directory / "reference.wav"
    path.write_bytes(b"original")
    voice = registry.create(
        voice_id="voice-one", model_id="model-one", label="Alice",
        relative_path="voices/voice-one/reference.wav", transcript=None,
    )
    replacement = directory / "replacement.wav"
    path.rename(replacement)
    path.write_bytes(b"replacement")
    service = SavedVoiceService(registry, ReferenceService(database, layout), layout)

    with pytest.raises(RuntimeError):
        service.delete(voice.id)
    assert registry.get(voice.id) == voice
    assert path.read_bytes() == b"replacement"


def test_saved_voice_delete_restores_exact_bytes_when_database_delete_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    registry = SavedVoiceRegistry(database)
    directory = layout.managed_child("voices", "voice-one")
    directory.mkdir()
    path = directory / "reference.wav"
    original = b"original saved voice bytes"
    path.write_bytes(original)
    voice = registry.create(
        voice_id="voice-one", model_id="model-one", label="Alice",
        relative_path="voices/voice-one/reference.wav", transcript=None,
    )

    def fail_delete(voice_id: str) -> object:
        raise sqlite3.OperationalError(f"database failure for {voice_id}")

    monkeypatch.setattr(registry, "delete", fail_delete)
    monkeypatch.setattr(voice_service, "unlink_open_file", _simulated_identity_unlink)
    service = SavedVoiceService(registry, ReferenceService(database, layout), layout)

    with pytest.raises(sqlite3.OperationalError, match="database failure"):
        service.delete(voice.id)

    assert registry.get(voice.id) == voice
    assert path.read_bytes() == original


def test_saved_voice_delete_does_not_unlink_replacement_after_identity_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    registry = SavedVoiceRegistry(database)
    directory = layout.managed_child("voices", "voice-one")
    directory.mkdir()
    path = directory / "reference.wav"
    path.write_bytes(b"original")
    voice = registry.create(
        voice_id="voice-one", model_id="model-one", label="Alice",
        relative_path="voices/voice-one/reference.wav", transcript=None,
    )
    moved = directory / "original-moved.wav"
    replacement = b"replacement"
    original_stat = os.stat
    replaced = False

    def replace_after_relative_stat(
        current_path: object, *args: object, **kwargs: object
    ) -> os.stat_result:
        nonlocal replaced
        metadata = original_stat(current_path, *args, **kwargs)
        if (
            not replaced
            and current_path == path.name
            and kwargs.get("dir_fd") is not None
            and kwargs.get("follow_symlinks") is False
        ):
            replaced = True
            path.rename(moved)
            path.write_bytes(replacement)
        return metadata

    monkeypatch.setattr(os, "stat", replace_after_relative_stat)
    service = SavedVoiceService(registry, ReferenceService(database, layout), layout)

    with pytest.raises(RuntimeError):
        service.delete(voice.id)

    assert registry.get(voice.id) == voice
    assert path.read_bytes() == replacement
    assert moved.read_bytes() == b"original"


def test_saved_voice_destination_swap_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    references = ReferenceService(database, layout)
    reference = references.create_upload(model_id="model-one", payload=b"reference")
    references.mark_validated(reference.id, ReferenceMetadata("wav", 16000, 1, 1))
    original_create = voice_service._create_voice_directory
    outside = tmp_path / "outside"
    outside.mkdir()

    def swap_directory(current_layout: StorageLayout, voice_id: str) -> tuple[int, int]:
        descriptors = original_create(current_layout, voice_id)
        directory = current_layout.managed_child("voices", voice_id)
        moved = tmp_path / "moved-voice"
        directory.rename(moved)
        directory.symlink_to(outside, target_is_directory=True)
        return descriptors

    monkeypatch.setattr(voice_service, "_create_voice_directory", swap_directory)
    monkeypatch.setattr(voice_service, "unlink_open_file", _simulated_identity_unlink)
    service = SavedVoiceService(SavedVoiceRegistry(database), references, layout)

    with pytest.raises(RuntimeError):
        service.create_from_reference(model_id="model-one", label="Alice", reference_id=reference.id)
    assert tuple(outside.iterdir()) == ()
    assert tuple((tmp_path / "moved-voice").iterdir()) == ()
    assert service.list_for_model("model-one") == ()


def test_saved_voice_publication_swap_removes_moved_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    references = ReferenceService(database, layout)
    reference = references.create_upload(model_id="model-one", payload=b"reference")
    references.mark_validated(reference.id, ReferenceMetadata("wav", 16000, 1, 1))
    registry = SavedVoiceRegistry(database)
    original_create = registry.create
    outside = tmp_path / "outside"
    outside.mkdir()

    def swap_after_row(**kwargs: object) -> object:
        voice = original_create(**kwargs)
        directory = layout.managed_child("voices", str(kwargs["voice_id"]))
        moved = tmp_path / "moved-after-row"
        directory.rename(moved)
        directory.symlink_to(outside, target_is_directory=True)
        return voice

    monkeypatch.setattr(registry, "create", swap_after_row)
    monkeypatch.setattr(voice_service, "unlink_open_file", _simulated_identity_unlink)
    service = SavedVoiceService(registry, references, layout)

    with pytest.raises(RuntimeError):
        service.create_from_reference(model_id="model-one", label="Alice", reference_id=reference.id)
    assert tuple(outside.iterdir()) == ()
    assert tuple((tmp_path / "moved-after-row").iterdir()) == ()
    assert service.list_for_model("model-one") == ()


def test_saved_voice_creation_retains_row_and_file_when_row_rollback_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    references = ReferenceService(database, layout)
    reference = references.create_upload(model_id="model-one", payload=b"reference")
    references.mark_validated(reference.id, ReferenceMetadata("wav", 16000, 1, 1))
    registry = SavedVoiceRegistry(database)
    def fail_delete(voice_id: str) -> object:
        raise OSError("row rollback failed")

    monkeypatch.setattr(registry, "delete", fail_delete)
    monkeypatch.setattr(references, "delete", lambda reference_id: (_ for _ in ()).throw(OSError("cleanup")))
    service = SavedVoiceService(registry, references, layout)

    with pytest.raises(RuntimeError, match="row and copied file were retained"):
        service.create_from_reference(model_id="model-one", label="Alice", reference_id=reference.id)
    voices = registry.list_for_model("model-one")
    assert len(voices) == 1
    assert (layout.root / Path(*voices[0].relative_path.split("/"))).read_bytes() == b"reference"


def test_saved_voice_creation_compensation_does_not_unlink_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    references = ReferenceService(database, layout)
    reference = references.create_upload(model_id="model-one", payload=b"original")
    references.mark_validated(reference.id, ReferenceMetadata("wav", 16000, 1, 1))
    registry = SavedVoiceRegistry(database)
    replacement_path: Path | None = None
    moved_path: Path | None = None

    def replace_then_fail(reference_id: str) -> object:
        nonlocal replacement_path, moved_path
        directory = next(layout.voices.iterdir())
        replacement_path = next(directory.iterdir())
        moved_path = directory / "original-moved.wav"
        replacement_path.rename(moved_path)
        replacement_path.write_bytes(b"replacement")
        raise OSError(f"reference cleanup failed for {reference_id}")

    monkeypatch.setattr(references, "delete", replace_then_fail)
    service = SavedVoiceService(registry, references, layout)

    with pytest.raises(OSError, match="reference cleanup failed"):
        service.create_from_reference(
            model_id="model-one", label="Alice", reference_id=reference.id
        )

    assert registry.list_for_model("model-one") == ()
    assert replacement_path is not None
    assert moved_path is not None
    assert replacement_path.read_bytes() == b"replacement"
    assert moved_path.read_bytes() == b"original"


def test_saved_voice_delete_rollback_fails_closed_on_directory_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    registry = SavedVoiceRegistry(database)
    directory = layout.managed_child("voices", "voice-one")
    directory.mkdir()
    path = directory / "reference.wav"
    path.write_bytes(b"original")
    voice = registry.create(
        voice_id="voice-one", model_id="model-one", label="Alice",
        relative_path="voices/voice-one/reference.wav", transcript=None,
    )
    outside = tmp_path / "outside"
    outside.mkdir()

    displaced = tmp_path / "displaced-voice"

    def fail_delete(voice_id: str) -> object:
        directory.rename(displaced)
        directory.symlink_to(outside, target_is_directory=True)
        raise OSError(f"database failure for {voice_id}")

    monkeypatch.setattr(registry, "delete", fail_delete)
    monkeypatch.setattr(voice_service, "unlink_open_file", _simulated_identity_unlink)
    service = SavedVoiceService(registry, ReferenceService(database, layout), layout)

    with pytest.raises(RuntimeError, match="could not be restored") as error:
        service.delete(voice.id)
    assert "database failure" in repr(error.value.__cause__)
    assert registry.get(voice.id) == voice
    assert tuple(outside.iterdir()) == ()


def test_saved_voice_creation_compensates_when_reference_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    references = ReferenceService(database, layout)
    reference = references.create_upload(model_id="model-one", payload=b"reference")
    references.mark_validated(reference.id, ReferenceMetadata("wav", 16000, 1, 1))
    monkeypatch.setattr(references, "delete", lambda reference_id: (_ for _ in ()).throw(OSError("cleanup")))
    monkeypatch.setattr(voice_service, "unlink_open_file", _simulated_identity_unlink)
    registry = SavedVoiceRegistry(database)
    service = SavedVoiceService(registry, references, layout)

    with pytest.raises(OSError, match="cleanup"):
        service.create_from_reference(model_id="model-one", label="Alice", reference_id=reference.id)
    assert registry.list_for_model("model-one") == ()
    assert tuple(layout.voices.iterdir()) == ()
