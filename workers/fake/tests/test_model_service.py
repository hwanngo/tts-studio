import hashlib
import os
from pathlib import Path

import pytest
import tts_studio_fake_worker.model_service as model_service_module
from tts_studio_fake_worker.model_service import FakeModelService
from tts_studio_protocol.engine.v1 import engine_pb2


def test_validation_distinguishes_compatible_incompatible_and_unknown_fixtures(
    tmp_path: Path,
) -> None:
    service = FakeModelService(tmp_path)

    compatible = service.validate_model(
        engine_pb2.ValidateModelRequest(repository_id="fixtures/compatible")
    )
    incompatible = service.validate_model(
        engine_pb2.ValidateModelRequest(repository_id="fixtures/incompatible")
    )
    unknown = service.validate_model(
        engine_pb2.ValidateModelRequest(repository_id="someone/private")
    )

    assert compatible.compatible is True
    assert compatible.resolved_commit == "1c6d281855eeb808859fc335a5ef01f66e82f4a3"
    assert incompatible.compatible is False
    assert incompatible.error.code == "model_incompatible"
    assert incompatible.evidence[0].code == "fake_fixture_incompatible"
    assert unknown.compatible is False
    assert unknown.repository_id == ""
    assert unknown.error.code == "model_incompatible"
    assert "someone/private" not in str(unknown)


def test_validation_rejects_an_unknown_revision_without_allocating_staging(
    tmp_path: Path,
) -> None:
    service = FakeModelService(tmp_path)

    response = service.validate_model(
        engine_pb2.ValidateModelRequest(
            repository_id="fixtures/compatible", requested_revision="missing"
        )
    )

    assert response.compatible is False
    assert response.error.code == "revision_not_found"
    assert tuple(tmp_path.iterdir()) == ()


@pytest.mark.asyncio
async def test_download_manifest_matches_files_and_checksum_failure_is_deterministic(
    tmp_path: Path,
) -> None:
    service = FakeModelService(tmp_path)
    compatible = tmp_path / "compatible-job"
    compatible.mkdir()

    events = [
        event
        async for event in service.download_model(
            engine_pb2.DownloadModelRequest(
                repository_id="fixtures/compatible",
                resolved_commit="1c6d281855eeb808859fc335a5ef01f66e82f4a3",
                variant="fp32",
                staging_destination=compatible.name,
            )
        )
    ]
    manifest = events[-1].manifest
    for item in manifest.files:
        artifact = compatible / item.relative_path
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == item.sha256

    failing = tmp_path / "checksum-job"
    failing.mkdir()
    failure_events = [
        event
        async for event in service.download_model(
            engine_pb2.DownloadModelRequest(
                repository_id="fixtures/checksum-failure",
                resolved_commit="d650fc66884d6c22e1988fab5be5e5d529da4704",
                variant="int8",
                staging_destination=failing.name,
            )
        )
    ]
    failing_manifest = failure_events[-1].manifest
    reported = {item.relative_path: item.sha256 for item in failing_manifest.files}
    assert reported["model.bin"] == "0" * 64
    assert hashlib.sha256((failing / "model.bin").read_bytes()).hexdigest() != "0" * 64


@pytest.mark.asyncio
async def test_download_rejects_traversal_and_symlink_destinations(
    tmp_path: Path,
) -> None:
    service = FakeModelService(tmp_path / "staging")
    outside = tmp_path / "outside"
    outside.mkdir()

    traversal = [
        event
        async for event in service.download_model(
            engine_pb2.DownloadModelRequest(
                repository_id="fixtures/compatible",
                resolved_commit="1c6d281855eeb808859fc335a5ef01f66e82f4a3",
                variant="int8",
                staging_destination="../outside",
            )
        )
    ]
    assert traversal[0].error.code == "download_failed"
    assert "outside" not in traversal[0].error.message

    linked = tmp_path / "staging" / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    symlinked = [
        event
        async for event in service.download_model(
            engine_pb2.DownloadModelRequest(
                repository_id="fixtures/compatible",
                resolved_commit="1c6d281855eeb808859fc335a5ef01f66e82f4a3",
                variant="int8",
                staging_destination="linked",
            )
        )
    ]
    assert symlinked[0].error.code == "download_failed"
    assert tuple(outside.iterdir()) == ()


@pytest.mark.asyncio
async def test_download_reports_a_safe_error_when_staging_is_not_empty(
    tmp_path: Path,
) -> None:
    service = FakeModelService(tmp_path)
    destination = tmp_path / "occupied"
    destination.mkdir()
    existing = destination / "config.json"
    existing.write_text("do not overwrite", encoding="utf-8")

    events = [
        event
        async for event in service.download_model(
            engine_pb2.DownloadModelRequest(
                repository_id="fixtures/compatible",
                resolved_commit="1c6d281855eeb808859fc335a5ef01f66e82f4a3",
                variant="int8",
                staging_destination=destination.name,
            )
        )
    ]

    assert events[-1].error.code == "download_failed"
    assert str(destination) not in events[-1].error.message
    assert existing.read_text(encoding="utf-8") == "do not overwrite"


@pytest.mark.asyncio
async def test_download_remains_anchored_when_validated_destination_is_swapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / "staging"
    service = FakeModelService(staging)
    destination = staging / "job"
    destination.mkdir()
    parked = staging / "parked"
    outside = tmp_path / "outside"
    outside.mkdir()
    real_open = model_service_module.os.open
    swapped = False

    def swap_parent_then_open(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and Path(path).name == "config.json":
            swapped = True
            destination.rename(parked)
            destination.symlink_to(outside, target_is_directory=True)
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(model_service_module.os, "open", swap_parent_then_open)

    events = [
        event
        async for event in service.download_model(
            engine_pb2.DownloadModelRequest(
                repository_id="fixtures/compatible",
                resolved_commit="1c6d281855eeb808859fc335a5ef01f66e82f4a3",
                variant="int8",
                staging_destination="job",
            )
        )
    ]

    assert swapped is True
    assert events[-1].WhichOneof("payload") == "manifest"
    assert tuple(outside.iterdir()) == ()
    assert {path.name for path in parked.iterdir()} == {"config.json", "model.bin"}
