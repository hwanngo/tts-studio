from pathlib import Path

import pytest

from tts_studio.references.domain import (
    InvalidReferenceTransitionError,
    ReferenceMetadata,
    ReferenceState,
)
from tts_studio.references.registry import ReferenceRegistry
from tts_studio.storage.db import Database
from tts_studio.storage.layout import StorageLayout


def _registry(tmp_path: Path) -> ReferenceRegistry:
    layout = StorageLayout.from_root(tmp_path / ".tts-studio")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    return ReferenceRegistry(database)


def test_registry_persists_metadata_without_transcript_or_bytes(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    created = registry.create_uploaded(
        reference_id="ref-1",
        model_id="model-1",
        relative_path="staging/references/ref-1",
        byte_size=3,
        sha256="a" * 64,
        transcript_present=True,
        expires_at="2026-09-07T01:00:00+00:00",
        now="2026-09-07T00:00:00+00:00",
    )

    assert created.state is ReferenceState.UPLOADED
    registry.mark_validated(
        "ref-1",
        ReferenceMetadata(container="wav", sample_rate_hz=16000, channels=1, duration_ms=100),
        now="2026-09-07T00:01:00+00:00",
    )
    claimed = registry.claim_for_generation("ref-1", now="2026-09-07T00:02:00+00:00")

    assert claimed.state is ReferenceState.CONSUMED
    with pytest.raises(InvalidReferenceTransitionError):
        registry.claim_for_generation("ref-1", now="2026-09-07T00:03:00+00:00")
    with registry._database.read() as connection:
        row = connection.execute("SELECT * FROM reference_recordings").fetchone()
    assert set(row.keys()) == {
        "id",
        "model_id",
        "relative_path",
        "byte_size",
        "sha256",
        "container",
        "sample_rate_hz",
        "channels",
        "duration_ms",
        "transcript_present",
        "state",
        "expires_at",
        "created_at",
        "updated_at",
    }
    assert all("transcript" not in str(value) for value in row)


def test_registry_expires_only_due_uploads_and_deletes_terminal_metadata(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    for identifier, expires_at in (
        ("due", "2026-09-07T00:00:00+00:00"),
        ("later", "2026-09-07T02:00:00+00:00"),
    ):
        registry.create_uploaded(
            reference_id=identifier,
            model_id="model",
            relative_path=f"staging/references/{identifier}",
            byte_size=1,
            sha256="b" * 64,
            transcript_present=False,
            expires_at=expires_at,
            now="2026-09-06T00:00:00+00:00",
        )

    expired = registry.expire(now="2026-09-07T00:00:00+00:00")
    assert [record.id for record in expired] == ["due"]
    assert registry.get("due").state is ReferenceState.EXPIRED
    assert registry.get("later").state is ReferenceState.UPLOADED
    registry.delete_metadata("due", now="2026-09-07T00:01:00+00:00")
    with pytest.raises(LookupError):
        registry.get("due")
