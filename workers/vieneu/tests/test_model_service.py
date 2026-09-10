from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import tts_studio_vieneu_worker.model_service as model_service_module
from huggingface_hub.errors import (
    LocalEntryNotFoundError,
    OfflineModeIsEnabled,
    RevisionNotFoundError,
)
from tts_studio_protocol.engine.v1 import engine_pb2
from tts_studio_vieneu_worker.constants import (
    MOSS_CODEC_COMMIT,
    MOSS_CODEC_REPOSITORY,
    VARIANT_DIRECTORIES,
    VIENEU_REPOSITORY,
)
from tts_studio_vieneu_worker.model_service import (
    CODEC_REPOSITORY,
    CODEC_REVISION,
    TARGET_REPOSITORY,
    VieNeuModelService,
    _remove_tree_no_follow,
)


def test_acquisition_policy_constants_are_canonical() -> None:
    assert VIENEU_REPOSITORY == TARGET_REPOSITORY
    assert MOSS_CODEC_REPOSITORY == CODEC_REPOSITORY
    assert MOSS_CODEC_COMMIT == CODEC_REVISION
    assert VARIANT_DIRECTORIES == {"int8": "onnx_int8", "fp32": "onnx_update"}


VARIANT_FILES = {
    "int8": (
        "config.json",
        "tokenizer.json",
        "vieneu_acoustic_cached.onnx",
        "vieneu_decode_step.onnx",
        "vieneu_prefill.onnx",
        "vieneu_backbone_shared.data",
        "vieneu_v3_heads.npz",
    ),
    "fp32": (
        "config.json",
        "tokenizer.json",
        "vieneu_acoustic_cached.onnx",
        "vieneu_decode_step.onnx",
        "vieneu_prefill.onnx",
        "vieneu_backbone_shared.data",
        "vieneu_v3_heads.npz",
    ),
}
CODEC_FILES = (
    "codec_browser_onnx_meta.json",
    "moss_audio_tokenizer_decode_full.onnx",
    "moss_audio_tokenizer_decode_shared.data",
    "moss_audio_tokenizer_decode_step.onnx",
    "moss_audio_tokenizer_encode.data",
    "moss_audio_tokenizer_encode.onnx",
)
MODEL_COMMIT = "a" * 40


def revision_not_found_error() -> RevisionNotFoundError:
    error = RevisionNotFoundError.__new__(RevisionNotFoundError)
    Exception.__init__(error, "requested revision is unavailable")
    return error


class FakeRepository:
    def __init__(
        self,
        files: dict[tuple[str, str], bytes],
        sha: str = MODEL_COMMIT,
        codec_sha: str = CODEC_REVISION,
    ) -> None:
        self.files = files
        self.sha = sha
        self.codec_sha = codec_sha
        self.size_override: object | None = None
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def model_info(self, repo_id: str, revision: str | None = None):
        self.calls.append(("model_info", (repo_id, revision)))
        if repo_id == CODEC_REPOSITORY:
            return SimpleNamespace(sha=self.codec_sha)
        return SimpleNamespace(sha=self.sha)

    def list_files(self, repo_id: str, revision: str):
        self.calls.append(("list_files", (repo_id, revision)))
        return [
            SimpleNamespace(
                rfilename=name,
                size=self.size_override if self.size_override is not None else len(content),
            )
            for (rid, _), names in self.files.items()
            if rid == repo_id
            for name, content in names.items()
        ]

    def snapshot_download(self, repo_id: str, revision: str, destination: Path, allow_patterns):
        self.calls.append(("snapshot_download", (repo_id, revision, destination, tuple(allow_patterns))))
        destination.mkdir(parents=True, exist_ok=True)
        for pattern in allow_patterns:
            content = self.files[(repo_id, revision)].get(pattern)
            if content is None:
                content = self.files[(repo_id, revision)][pattern.split("/", 1)[-1]]
            target = destination / pattern
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)


def repository() -> FakeRepository:
    files: dict[tuple[str, str], bytes] = {}
    model_files: dict[str, bytes] = {}
    for variant, names in VARIANT_FILES.items():
        model_files.update({
            f"onnx_{variant if variant == 'int8' else 'update'}/{name}": f"{variant}:{name}".encode()
            for name in names
        })
    model_files.update({name: f"root:{name}".encode() for name in ("denoiser.onnx", "speaker_encoder.onnx")})
    files[(TARGET_REPOSITORY, MODEL_COMMIT)] = model_files
    files[(CODEC_REPOSITORY, CODEC_REVISION)] = {name: f"codec:{name}".encode() for name in CODEC_FILES}
    return FakeRepository(files)


def test_validation_resolves_default_and_requested_immutable_revision_for_both_variants(tmp_path: Path) -> None:
    client = repository()
    service = VieNeuModelService(client, tmp_path)

    default = service.validate(engine_pb2.ValidateModelRequest(repository_id=TARGET_REPOSITORY))
    requested = service.validate(
        engine_pb2.ValidateModelRequest(repository_id=TARGET_REPOSITORY, requested_revision=MODEL_COMMIT)
    )

    assert default.compatible and default.resolved_commit == MODEL_COMMIT
    assert requested.compatible and requested.resolved_commit == MODEL_COMMIT
    assert default.estimated_bytes == sum(len(content) for content in client.files[(TARGET_REPOSITORY, MODEL_COMMIT)].values()) + sum(
        len(content) for content in client.files[(CODEC_REPOSITORY, CODEC_REVISION)].values()
    )
    assert {variant.id for variant in default.available_variants} == {"int8", "fp32"}
    assert set(default.required_files) == {
        *(f"backbone/{variant}/{name}" for variant in ("int8", "fp32") for name in VARIANT_FILES[variant]),
        "cloning/denoiser.onnx",
        "cloning/speaker_encoder.onnx",
        *(f"codec/{name}" for name in CODEC_FILES),
    }
    assert any(
        e.code == "codec_provenance"
        and CODEC_REPOSITORY in e.message
        and CODEC_REVISION in e.message
        for e in default.evidence
    )
    assert all(call[0] != "snapshot_download" for call in client.calls)


def test_validation_rejects_explicit_commit_that_resolves_to_a_different_sha(tmp_path: Path) -> None:
    response = VieNeuModelService(repository(), tmp_path).validate(
        engine_pb2.ValidateModelRequest(repository_id=TARGET_REPOSITORY, requested_revision="b" * 40)
    )

    assert not response.compatible
    assert response.error.code == "revision_not_found"
    assert not response.error.retryable


@pytest.mark.parametrize(
    ("failure", "code", "retryable"),
    [
        (
            revision_not_found_error(),
            "revision_not_found",
            False,
        ),
        (LocalEntryNotFoundError("offline cache miss"), "download_failed", True),
        (OfflineModeIsEnabled(), "download_failed", True),
        (httpx.ConnectError("connection refused"), "download_failed", True),
        (httpx.ReadTimeout("timed out"), "download_failed", True),
        (httpx.RemoteProtocolError("peer closed connection"), "download_failed", True),
    ],
)
def test_validation_preserves_repository_error_classification(
    tmp_path: Path, failure: Exception, code: str, retryable: bool
) -> None:
    client = repository()

    def model_info(repo_id: str, revision: str | None = None):
        raise failure

    client.model_info = model_info
    response = VieNeuModelService(client, tmp_path).validate(
        engine_pb2.ValidateModelRequest(repository_id=TARGET_REPOSITORY)
    )

    assert not response.compatible
    assert response.error.code == code
    assert response.error.retryable is retryable


def test_validation_rejects_unknown_repository_and_missing_variant_or_codec_files(tmp_path: Path) -> None:
    client = repository()
    service = VieNeuModelService(client, tmp_path)

    unknown = service.validate(engine_pb2.ValidateModelRequest(repository_id="someone/else"))
    assert not unknown.compatible
    assert unknown.error.code == "model_incompatible"
    assert "someone/else" not in unknown.error.message
    assert "traceback" not in unknown.error.message.lower()

    del client.files[(TARGET_REPOSITORY, MODEL_COMMIT)]["onnx_int8/vieneu_prefill.onnx"]
    missing = service.validate(engine_pb2.ValidateModelRequest(repository_id=TARGET_REPOSITORY))
    assert not missing.compatible
    assert missing.error.code == "model_incompatible"
    assert "vieneu_prefill.onnx" in missing.error.details["missing_file"]


@pytest.mark.parametrize("invalid_sha", ["main", "model-commit", "A" * 40, "a" * 39, "a" * 65])
def test_validation_rejects_non_immutable_repository_revision(tmp_path: Path, invalid_sha: str) -> None:
    client = repository()
    client.sha = invalid_sha

    response = VieNeuModelService(client, tmp_path).validate(
        engine_pb2.ValidateModelRequest(repository_id=TARGET_REPOSITORY)
    )

    assert not response.compatible
    assert response.error.code == "model_incompatible"
    assert invalid_sha not in response.error.message


def test_validation_handles_malformed_repository_size_metadata_safely(tmp_path: Path) -> None:
    client = repository()
    client.size_override = "not-a-size"

    response = VieNeuModelService(client, tmp_path).validate(
        engine_pb2.ValidateModelRequest(repository_id=TARGET_REPOSITORY)
    )

    assert not response.compatible
    assert response.error.code == "model_incompatible"
    assert "not-a-size" not in response.error.message


def test_cleanup_does_not_follow_a_directory_replaced_after_identity_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "owned"
    replaced = root / "nested"
    replaced.mkdir(parents=True)
    outside = tmp_path / "outside-cleanup"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"outside sentinel")
    original_lstat = Path.lstat
    original_open = os.open
    swapped = False

    def lstat(path: Path):
        nonlocal swapped
        metadata = original_lstat(path)
        if path == replaced and not swapped:
            swapped = True
            replaced.rename(tmp_path / "nested-original")
            replaced.symlink_to(outside, target_is_directory=True)
        return metadata

    def open_path(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == replaced.name and dir_fd is not None and not swapped:
            swapped = True
            replaced.rename(tmp_path / "nested-original")
            replaced.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    directory_flags = model_service_module._directory_open_flags()
    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(model_service_module, "_directory_open_flags", lambda: directory_flags)
    monkeypatch.setattr(os, "open", open_path)

    _remove_tree_no_follow(root)

    assert swapped is True
    assert sentinel.read_bytes() == b"outside sentinel"


@pytest.mark.asyncio
async def test_download_stages_complete_manifest_with_ordered_progress_and_pinned_codec(
    tmp_path: Path,
) -> None:
    client = repository()
    service = VieNeuModelService(client, tmp_path)
    request = engine_pb2.DownloadModelRequest(
        repository_id=TARGET_REPOSITORY,
        resolved_commit=MODEL_COMMIT,
        variant="int8",
        staging_destination="job-1",
    )

    events = [event async for event in service.download(request, _Context())]

    assert [event.WhichOneof("payload") for event in events][-1] == "manifest"
    progress = [event.progress for event in events if event.HasField("progress")]
    assert [item.sequence for item in progress] == sorted(item.sequence for item in progress)
    assert progress[-1].phase == engine_pb2.DOWNLOAD_PHASE_FINALIZING
    manifest = events[-1].manifest
    expected = {
        *(f"backbone/{variant}/{name}" for variant in ("int8", "fp32") for name in VARIANT_FILES[variant]),
        "cloning/denoiser.onnx",
        "cloning/speaker_encoder.onnx",
        *(f"codec/{name}" for name in CODEC_FILES),
    }
    assert {item.relative_path for item in manifest.files} == expected
    validation = service.validate(engine_pb2.ValidateModelRequest(repository_id=TARGET_REPOSITORY))
    activation_required_files = set(validation.required_files)
    manifest_paths = {item.relative_path for item in manifest.files}
    assert activation_required_files == manifest_paths
    assert activation_required_files.issubset(manifest_paths)
    assert manifest.byte_size == sum(item.byte_size for item in manifest.files)
    assert all("/" not in item.relative_path[:0] and not Path(item.relative_path).is_absolute() for item in manifest.files)
    assert (tmp_path / "staging" / "job-1" / "backbone/int8/config.json").is_file()
    codec_call = next(call for call in client.calls if call[0] == "snapshot_download" and call[1][0] == CODEC_REPOSITORY)
    assert codec_call[1][1] == CODEC_REVISION
    assert all(item.sha256 == hashlib.sha256((tmp_path / "staging" / "job-1" / item.relative_path).read_bytes()).hexdigest() for item in manifest.files)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "code", "retryable"),
    [
        (
            revision_not_found_error(),
            "revision_not_found",
            False,
        ),
        (LocalEntryNotFoundError("offline cache miss"), "download_failed", True),
        (OfflineModeIsEnabled(), "download_failed", True),
        (httpx.ConnectError("connection refused"), "download_failed", True),
        (httpx.ReadTimeout("timed out"), "download_failed", True),
        (httpx.RemoteProtocolError("peer closed connection"), "download_failed", True),
    ],
)
async def test_download_preserves_repository_error_classification(
    tmp_path: Path, failure: Exception, code: str, retryable: bool
) -> None:
    client = repository()

    def model_info(repo_id: str, revision: str | None = None):
        raise failure

    client.model_info = model_info
    request = engine_pb2.DownloadModelRequest(
        repository_id=TARGET_REPOSITORY,
        resolved_commit=MODEL_COMMIT,
        variant="int8",
        staging_destination="repository-failure",
    )

    events = [event async for event in VieNeuModelService(client, tmp_path).download(request, _Context())]

    assert events[-1].error.code == code
    assert events[-1].error.retryable is retryable


@pytest.mark.asyncio
async def test_download_fails_closed_when_staging_root_is_replaced_after_snapshots(
    tmp_path: Path,
) -> None:
    client = repository()
    outside = tmp_path / "outside"
    outside.mkdir()
    replaced = False

    original_model_info = client.model_info

    def model_info(repo_id: str, revision: str | None = None):
        nonlocal replaced
        result = original_model_info(repo_id, revision)
        if repo_id == TARGET_REPOSITORY and not replaced:
            replaced = True
            staging = tmp_path / "staging"
            staging.rename(tmp_path / "staging-original")
            staging.symlink_to(outside, target_is_directory=True)
        return result

    client.model_info = model_info
    request = engine_pb2.DownloadModelRequest(
        repository_id=TARGET_REPOSITORY,
        resolved_commit=MODEL_COMMIT,
        variant="int8",
        staging_destination="replaced-root",
    )

    events = [event async for event in VieNeuModelService(client, tmp_path).download(request, _Context())]

    assert events[-1].error.code == "download_failed"
    assert not any(path.is_file() for path in outside.rglob("*"))


@pytest.mark.asyncio
async def test_download_does_not_write_outside_when_root_is_replaced_during_second_snapshot(
    tmp_path: Path,
) -> None:
    client = repository()
    outside = tmp_path / "outside-during-second-snapshot"
    outside.mkdir()
    staging = tmp_path / "staging"
    sentinel = outside / "sentinel"
    sentinel_path = sentinel
    replaced = False
    original_snapshot = client.snapshot_download

    def snapshot_download(repo_id: str, revision: str, destination: Path, allow_patterns):
        nonlocal replaced, sentinel_path
        if repo_id == CODEC_REPOSITORY and not replaced:
            replaced = True
            if destination.is_relative_to(staging):
                redirected_destination = outside / destination.relative_to(staging)
                sentinel_target = redirected_destination / CODEC_FILES[0]
            else:
                sentinel_target = sentinel
            sentinel_path = sentinel_target
            sentinel_target.parent.mkdir(parents=True, exist_ok=True)
            sentinel_target.write_bytes(b"outside sentinel")
            staging.rename(tmp_path / "staging-original")
            staging.symlink_to(outside, target_is_directory=True)
        return original_snapshot(repo_id, revision, destination, allow_patterns)

    client.snapshot_download = snapshot_download
    request = engine_pb2.DownloadModelRequest(
        repository_id=TARGET_REPOSITORY,
        resolved_commit=MODEL_COMMIT,
        variant="int8",
        staging_destination="second-snapshot-race",
    )

    events = [
        event
        async for event in VieNeuModelService(client, tmp_path).download(request, _Context())
    ]

    assert events[-1].error.code == "download_failed"
    assert sentinel_path.read_bytes() == b"outside sentinel"


@pytest.mark.asyncio
async def test_download_does_not_write_outside_when_scratch_workdir_is_replaced_during_second_snapshot(
    tmp_path: Path,
) -> None:
    client = repository()
    outside = tmp_path / "outside-during-scratch-race"
    outside.mkdir()
    scratch_root = tmp_path / ".vieneu-downloads"
    first_destination: Path | None = None
    sentinel = outside / "codec" / CODEC_FILES[0]
    replaced = False
    original_snapshot = client.snapshot_download

    def snapshot_download(repo_id: str, revision: str, destination: Path, allow_patterns):
        nonlocal first_destination, replaced
        if repo_id == TARGET_REPOSITORY:
            first_destination = destination
        if repo_id == CODEC_REPOSITORY and not replaced:
            replaced = True
            candidates = list(scratch_root.glob(".repository-download-*"))
            assert first_destination is not None
            work = candidates[0] if candidates else first_destination.parent
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_bytes(b"outside sentinel")
            shutil.rmtree(work)
            work.symlink_to(outside, target_is_directory=True)
        return original_snapshot(repo_id, revision, destination, allow_patterns)

    client.snapshot_download = snapshot_download
    request = engine_pb2.DownloadModelRequest(
        repository_id=TARGET_REPOSITORY,
        resolved_commit=MODEL_COMMIT,
        variant="int8",
        staging_destination="scratch-workdir-race",
    )

    events = [event async for event in VieNeuModelService(client, tmp_path).download(request, _Context())]

    assert events[-1].error.code == "download_failed"
    assert sentinel.read_bytes() == b"outside sentinel"


@pytest.mark.asyncio
async def test_download_does_not_write_outside_when_scratch_root_is_replaced_during_second_snapshot(
    tmp_path: Path,
) -> None:
    client = repository()
    scratch_root = tmp_path / ".vieneu-downloads"
    outside = tmp_path / "outside-during-scratch-root-race"
    outside.mkdir()
    sentinel: Path | None = None
    replaced = False
    original_snapshot = client.snapshot_download

    def snapshot_download(repo_id: str, revision: str, destination: Path, allow_patterns):
        nonlocal replaced, sentinel
        if repo_id == CODEC_REPOSITORY and not replaced:
            replaced = True
            candidates = list(scratch_root.glob(".repository-download-*"))
            assert len(candidates) == 1
            redirected_destination = outside / candidates[0].name / "codec"
            sentinel = redirected_destination / CODEC_FILES[0]
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_bytes(b"outside sentinel")
            scratch_root.rename(tmp_path / ".vieneu-downloads-original")
            scratch_root.symlink_to(outside, target_is_directory=True)
        return original_snapshot(repo_id, revision, destination, allow_patterns)

    client.snapshot_download = snapshot_download
    request = engine_pb2.DownloadModelRequest(
        repository_id=TARGET_REPOSITORY,
        resolved_commit=MODEL_COMMIT,
        variant="int8",
        staging_destination="scratch-root-race",
    )

    events = [event async for event in VieNeuModelService(client, tmp_path).download(request, _Context())]

    assert events[-1].error.code == "download_failed"
    assert sentinel is not None
    assert sentinel.read_bytes() == b"outside sentinel"


@pytest.mark.asyncio
async def test_snapshot_destinations_cannot_redirect_an_actual_sdk_write_outside_data_root(
    tmp_path: Path,
) -> None:
    client = repository()
    outside = tmp_path / "outside-sdk-destination"
    outside.mkdir()
    sentinel = outside / "onnx_int8" / VARIANT_FILES["int8"][0]
    sentinel.parent.mkdir(parents=True)
    sentinel.write_bytes(b"outside sentinel")
    original_snapshot = client.snapshot_download
    destinations: list[Path] = []

    def snapshot_download(repo_id: str, revision: str, destination: Path, allow_patterns):
        destinations.append(destination)
        if destination in (Path("model"), Path("codec")):
            redirected = Path.cwd() / destination
            redirected.symlink_to(outside, target_is_directory=True)
        return original_snapshot(repo_id, revision, destination, allow_patterns)

    client.snapshot_download = snapshot_download
    request = engine_pb2.DownloadModelRequest(
        repository_id=TARGET_REPOSITORY,
        resolved_commit=MODEL_COMMIT,
        variant="int8",
        staging_destination="sdk-destination-race",
    )

    events = [event async for event in VieNeuModelService(client, tmp_path).download(request, _Context())]

    assert events[-1].HasField("manifest")
    assert sentinel.read_bytes() == b"outside sentinel"
    assert destinations == [Path("."), Path(".")]


@pytest.mark.asyncio
async def test_download_rejects_unsafe_staging_and_honors_cancellation(tmp_path: Path) -> None:
    client = repository()
    service = VieNeuModelService(client, tmp_path)
    unsafe = engine_pb2.DownloadModelRequest(
        repository_id=TARGET_REPOSITORY, resolved_commit=MODEL_COMMIT, variant="fp32", staging_destination="../escape"
    )
    events = [event async for event in service.download(unsafe, _Context())]
    assert events[0].error.code == "invalid_request"
    assert not (tmp_path / "escape").exists()

    context = _Context(cancelled=True)
    cancellable = engine_pb2.DownloadModelRequest(
        repository_id=TARGET_REPOSITORY,
        resolved_commit=MODEL_COMMIT,
        variant="fp32",
        staging_destination="job-2",
    )
    cancelled = [event async for event in service.download(cancellable, context)]
    assert cancelled[0].error.code == "download_cancelled"

    backslash = engine_pb2.DownloadModelRequest(
        repository_id=TARGET_REPOSITORY,
        resolved_commit=MODEL_COMMIT,
        variant="fp32",
        staging_destination="job\\escaped",
    )
    events = [event async for event in service.download(backslash, _Context())]
    assert events[0].error.code == "invalid_request"


@pytest.mark.asyncio
async def test_download_handles_non_immutable_repository_revision_safely(tmp_path: Path) -> None:
    client = repository()
    client.sha = "main"
    request = engine_pb2.DownloadModelRequest(
        repository_id=TARGET_REPOSITORY,
        resolved_commit="main",
        variant="int8",
        staging_destination="invalid-revision",
    )

    events = [event async for event in VieNeuModelService(client, tmp_path).download(request, _Context())]

    assert events[-1].error.code == "download_failed"
    assert not events[-1].error.retryable
    assert "main" not in events[-1].error.message


def test_rejects_symlinked_explicit_data_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(ValueError, match="data root"):
        VieNeuModelService(repository(), redirected)


def test_validation_rejects_unpinned_codec_revision(tmp_path: Path) -> None:
    service = VieNeuModelService(repository_with_codec_sha("wrong-codec-commit"), tmp_path)

    response = service.validate(engine_pb2.ValidateModelRequest(repository_id=TARGET_REPOSITORY))

    assert not response.compatible
    assert response.error.code == "model_incompatible"
    assert "wrong-codec-commit" not in response.error.message


def test_validation_rejects_backslash_repository_paths(tmp_path: Path) -> None:
    client = repository()
    client.files[(TARGET_REPOSITORY, MODEL_COMMIT)]["onnx_int8\\config.json"] = b"bad"
    service = VieNeuModelService(client, tmp_path)

    response = service.validate(engine_pb2.ValidateModelRequest(repository_id=TARGET_REPOSITORY))

    assert not response.compatible
    assert response.error.code == "model_incompatible"


def repository_with_codec_sha(sha: str) -> FakeRepository:
    client = repository()
    client.codec_sha = sha
    return client


@pytest.mark.asyncio
async def test_cancellation_during_blocking_snapshot_cleans_partial_state(tmp_path: Path) -> None:
    client = repository()
    started = asyncio.Event()
    release = asyncio.Event()
    original = client.snapshot_download
    loop = asyncio.get_running_loop()

    def blocking_snapshot(repo_id: str, revision: str, destination: Path, allow_patterns):
        loop.call_soon_threadsafe(started.set)
        while not release.is_set():
            time.sleep(0.005)
        original(repo_id, revision, destination, allow_patterns)

    client.snapshot_download = blocking_snapshot
    service = VieNeuModelService(client, tmp_path)
    request = engine_pb2.DownloadModelRequest(
        repository_id=TARGET_REPOSITORY,
        resolved_commit=MODEL_COMMIT,
        variant="int8",
        staging_destination="cancelled-transfer",
    )
    stream = service.download(request, _Context())
    await stream.__anext__()
    task = asyncio.create_task(stream.__anext__())
    await started.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (tmp_path / "staging" / "cancelled-transfer").exists()


@pytest.mark.asyncio
async def test_cancellation_does_not_remove_replaced_current_job_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = repository()
    context = _Context()
    original_copy = model_service_module._copy_regular_file
    replaced = False
    protected = tmp_path / "staging" / "replaced-root" / "protected.bin"

    def copy_then_replace_root(source: Path, target: Path, source_root: Path, target_root: Path):
        nonlocal replaced
        result = original_copy(source, target, source_root, target_root)
        if not replaced:
            replaced = True
            context._cancelled = True
            shutil.rmtree(target_root.path)
            target_root.path.mkdir(parents=True)
            replacement_target = target_root.path / "backbone" / "int8" / "config.json"
            replacement_target.parent.mkdir(parents=True)
            replacement_target.write_bytes(b"replacement target")
            protected.write_bytes(b"replacement must survive")
        return result

    monkeypatch.setattr(model_service_module, "_copy_regular_file", copy_then_replace_root)
    request = engine_pb2.DownloadModelRequest(
        repository_id=TARGET_REPOSITORY,
        resolved_commit=MODEL_COMMIT,
        variant="int8",
        staging_destination="replaced-root",
    )

    events = [event async for event in VieNeuModelService(client, tmp_path).download(request, context)]

    assert events[-1].error.code == "download_cancelled"
    assert protected.read_bytes() == b"replacement must survive"


@pytest.mark.asyncio
async def test_cancellation_does_not_follow_replaced_current_job_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = repository()
    context = _Context()
    original_copy = model_service_module._copy_regular_file
    replaced = False
    outside = tmp_path / "outside"
    replacement_target = outside / "backbone" / "int8" / "config.json"
    replacement_target.parent.mkdir(parents=True)
    replacement_target.write_bytes(b"symlink replacement must survive")

    def copy_then_replace_root(source: Path, target: Path, source_root: Path, target_root: Path):
        nonlocal replaced
        result = original_copy(source, target, source_root, target_root)
        if not replaced:
            replaced = True
            context._cancelled = True
            shutil.rmtree(target_root.path)
            target_root.path.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(model_service_module, "_copy_regular_file", copy_then_replace_root)
    request = engine_pb2.DownloadModelRequest(
        repository_id=TARGET_REPOSITORY,
        resolved_commit=MODEL_COMMIT,
        variant="int8",
        staging_destination="replaced-symlink-root",
    )

    events = [event async for event in VieNeuModelService(client, tmp_path).download(request, context)]

    assert events[-1].error.code == "download_cancelled"
    assert replacement_target.read_bytes() == b"symlink replacement must survive"


@pytest.mark.asyncio
async def test_cancellation_does_not_allow_blocking_snapshot_to_recreate_job_staging(
    tmp_path: Path,
) -> None:
    client = repository()
    started = asyncio.Event()
    finished = asyncio.Event()
    release = asyncio.Event()
    original = client.snapshot_download
    loop = asyncio.get_running_loop()

    def blocking_snapshot(repo_id: str, revision: str, destination: Path, allow_patterns):
        loop.call_soon_threadsafe(started.set)
        while not release.is_set():
            time.sleep(0.005)
        original(repo_id, revision, destination, allow_patterns)
        loop.call_soon_threadsafe(finished.set)

    client.snapshot_download = blocking_snapshot
    service = VieNeuModelService(client, tmp_path)
    request = engine_pb2.DownloadModelRequest(
        repository_id=TARGET_REPOSITORY,
        resolved_commit=MODEL_COMMIT,
        variant="int8",
        staging_destination="detached-snapshot",
    )
    stream = service.download(request, _Context())
    await stream.__anext__()
    task = asyncio.create_task(stream.__anext__())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.2)
    assert not (tmp_path / "staging" / "detached-snapshot").exists()

    release.set()
    await asyncio.wait_for(finished.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not (tmp_path / "staging" / "detached-snapshot").exists()


@pytest.mark.asyncio
async def test_cancellation_during_blocking_repository_metadata_does_not_wait_for_thread(
    tmp_path: Path,
) -> None:
    client = repository()
    started = asyncio.Event()
    release = asyncio.Event()
    loop = asyncio.get_running_loop()
    original = client.model_info

    def blocking_model_info(repo_id: str, revision: str | None = None):
        loop.call_soon_threadsafe(started.set)
        while not release.is_set():
            time.sleep(0.005)
        return original(repo_id, revision)

    client.model_info = blocking_model_info
    service = VieNeuModelService(client, tmp_path)
    request = engine_pb2.DownloadModelRequest(
        repository_id=TARGET_REPOSITORY,
        resolved_commit=MODEL_COMMIT,
        variant="int8",
        staging_destination="cancelled-metadata",
    )
    stream = service.download(request, _Context())
    task = asyncio.create_task(stream.__anext__())
    await started.wait()
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.2)
    finally:
        release.set()
    assert not (tmp_path / "staging" / "cancelled-metadata").exists()


@pytest.mark.asyncio
async def test_failed_download_preserves_preexisting_staging_content(tmp_path: Path) -> None:
    client = repository()
    staging = tmp_path / "staging" / "retry-transfer"
    staging.mkdir(parents=True)
    (staging / "keep.txt").write_text("keep me")
    old_work = staging / ".repository-download"
    old_work.mkdir()
    (old_work / "old-partial.bin").write_bytes(b"old partial")

    def failing_snapshot(repo_id: str, revision: str, destination: Path, allow_patterns):
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "partial.bin").write_bytes(b"request partial")
        raise OSError("simulated transfer failure")

    client.snapshot_download = failing_snapshot
    request = engine_pb2.DownloadModelRequest(
        repository_id=TARGET_REPOSITORY,
        resolved_commit=MODEL_COMMIT,
        variant="int8",
        staging_destination="retry-transfer",
    )

    events = [event async for event in VieNeuModelService(client, tmp_path).download(request, _Context())]

    assert events[-1].error.code == "download_failed"
    assert events[-1].error.retryable
    assert (staging / "keep.txt").read_text() == "keep me"
    assert (old_work / "old-partial.bin").read_bytes() == b"old partial"
    assert sorted(staging.glob(".repository-download-*")) == []


@pytest.mark.asyncio
async def test_cancellation_cleanup_does_not_follow_replaced_staging_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = repository()
    context = _Context()
    original_copy = model_service_module._copy_regular_file
    copied = False
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "int8").mkdir()
    protected = outside / "int8" / "config.json"
    protected.write_bytes(b"protected")

    def copy_then_redirect(source: Path, target: Path, source_root: Path, target_root: Path):
        nonlocal copied
        result = original_copy(source, target, source_root, target_root)
        if not copied:
            copied = True
            context._cancelled = True
            backbone = target_root.path / "backbone"
            shutil.rmtree(backbone)
            backbone.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(model_service_module, "_copy_regular_file", copy_then_redirect)
    request = engine_pb2.DownloadModelRequest(
        repository_id=TARGET_REPOSITORY,
        resolved_commit=MODEL_COMMIT,
        variant="int8",
        staging_destination="redirected-cleanup",
    )

    events = [event async for event in VieNeuModelService(client, tmp_path).download(request, context)]

    assert events[-1].error.code == "download_cancelled"
    assert protected.read_bytes() == b"protected"


def test_validation_rejects_duplicate_or_unsafe_repository_listing(tmp_path: Path) -> None:
    client = repository()
    original = client.list_files

    def duplicate_listing(repo_id: str, revision: str):
        entries = list(original(repo_id, revision))
        if repo_id == TARGET_REPOSITORY:
            entries.append(entries[0])
        return entries

    client.list_files = duplicate_listing
    response = VieNeuModelService(client, tmp_path).validate(
        engine_pb2.ValidateModelRequest(repository_id=TARGET_REPOSITORY)
    )
    assert not response.compatible
    assert response.error.code == "model_incompatible"

    client = repository()
    original = client.list_files

    def unsafe_listing(repo_id: str, revision: str):
        entries = list(original(repo_id, revision))
        if repo_id == TARGET_REPOSITORY:
            entries.append(SimpleNamespace(rfilename="../escape.onnx"))
        return entries

    client.list_files = unsafe_listing
    response = VieNeuModelService(client, tmp_path).validate(
        engine_pb2.ValidateModelRequest(repository_id=TARGET_REPOSITORY)
    )
    assert not response.compatible
    assert response.error.code == "model_incompatible"


@pytest.mark.asyncio
async def test_download_rejects_symlinked_promotion_parent(tmp_path: Path) -> None:
    client = repository()
    service = VieNeuModelService(client, tmp_path)
    destination = tmp_path / "outside"
    destination.mkdir()
    staging = tmp_path / "staging" / "job-3"
    staging.mkdir(parents=True)
    (staging / "backbone").symlink_to(destination, target_is_directory=True)
    request = engine_pb2.DownloadModelRequest(
        repository_id=TARGET_REPOSITORY,
        resolved_commit=MODEL_COMMIT,
        variant="int8",
        staging_destination="job-3",
    )

    events = [event async for event in service.download(request, _Context())]

    assert events[-1].error.code == "download_failed"
    assert not (destination / "int8" / "config.json").exists()


class _Context:
    def __init__(self, cancelled: bool = False) -> None:
        self._cancelled = cancelled

    def cancelled(self) -> bool:
        return self._cancelled
