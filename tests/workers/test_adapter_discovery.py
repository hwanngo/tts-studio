import asyncio
import hashlib
import os
import sys
from pathlib import Path

import grpc
import pytest
from tts_studio_protocol.engine.v1 import engine_pb2

from tts_studio.storage.layout import StorageLayout
from tts_studio.workers.adapters import discover_adapters
from tts_studio.workers.supervisor import WorkerSupervisor

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _write_shadow_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
    path.chmod(0o700)


def _write_verified_worker_environment(environment: Path) -> Path:
    bin_directory = environment / "bin"
    site_packages = (
        environment
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    bin_directory.mkdir(parents=True)
    site_packages.mkdir(parents=True)
    (environment / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    python = bin_directory / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o700)
    worker = bin_directory / "tts-studio-fake-worker"
    worker.write_text(
        f"#!/bin/sh\n'''exec' '{python}' \"$0\" \"$@\"\n",
        encoding="utf-8",
    )
    worker.chmod(0o700)

    distributions = {
        "tts_studio_fake_worker-0.1.0.dist-info": (
            (
                "Name: tts-studio-fake-worker\n"
                "Version: 0.1.0\n"
                "Requires-Dist: tts-studio-protocol\n"
                "Requires-Dist: tts-studio-worker-sdk\n"
            ),
            (
                "[console_scripts]\n"
                "tts-studio-fake-worker = tts_studio_fake_worker.main:main\n"
            ),
        ),
        "tts_studio_protocol-0.1.0.dist-info": (
            "Name: tts-studio-protocol\nVersion: 0.1.0\n",
            "",
        ),
        "tts_studio_worker_sdk-0.1.0.dist-info": (
            "Name: tts-studio-worker-sdk\nVersion: 0.1.0\n",
            "",
        ),
    }
    for directory_name, (metadata, entry_points) in distributions.items():
        directory = site_packages / directory_name
        directory.mkdir()
        (directory / "METADATA").write_text(metadata, encoding="utf-8")
        (directory / "INSTALLER").write_text("uv\n", encoding="utf-8")
        (directory / "direct_url.json").write_text(
            '{"url":"file:///tmp/tts-studio-worker.whl","archive_info":{}}',
            encoding="utf-8",
        )
        if entry_points:
            (directory / "entry_points.txt").write_text(entry_points, encoding="utf-8")
    return worker


def _write_verified_vieneu_worker_environment(environment: Path) -> Path:
    bin_directory = environment / "bin"
    site_packages = (
        environment
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    bin_directory.mkdir(parents=True)
    site_packages.mkdir(parents=True)
    (environment / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    python = bin_directory / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o700)
    worker = bin_directory / "tts-studio-vieneu-worker"
    worker.write_text(
        f"#!/bin/sh\n'''exec' '{python}' \"$0\" \"$@\"\n",
        encoding="utf-8",
    )
    worker.chmod(0o700)

    distributions = {
        "tts_studio_vieneu_worker-0.1.0.dist-info": (
            (
                "Name: tts-studio-vieneu-worker\n"
                "Version: 0.1.0\n"
                "Requires-Dist: tts-studio-protocol\n"
                "Requires-Dist: tts-studio-worker-sdk\n"
                "Requires-Dist: vieneu==3.6.3\n"
            ),
            (
                "[console_scripts]\n"
                "tts-studio-vieneu-worker = tts_studio_vieneu_worker.main:main\n"
            ),
        ),
        "tts_studio_protocol-0.1.0.dist-info": (
            "Name: tts-studio-protocol\nVersion: 0.1.0\n",
            "",
        ),
        "tts_studio_worker_sdk-0.1.0.dist-info": (
            "Name: tts-studio-worker-sdk\nVersion: 0.1.0\n",
            "",
        ),
        "vieneu-3.6.3.dist-info": (
            "Name: vieneu\nVersion: 3.6.3\n",
            "",
        ),
    }
    for directory_name, (metadata_text, entry_points) in distributions.items():
        directory = site_packages / directory_name
        directory.mkdir()
        (directory / "METADATA").write_text(metadata_text, encoding="utf-8")
        (directory / "INSTALLER").write_text("uv\n", encoding="utf-8")
        (directory / "direct_url.json").write_text(
            '{"url":"file:///tmp/tts-studio-worker.whl","archive_info":{}}',
            encoding="utf-8",
        )
        if entry_points:
            (directory / "entry_points.txt").write_text(entry_points, encoding="utf-8")
    return worker


@pytest.fixture
async def fake_adapter(tmp_path: Path):
    layout = StorageLayout.from_root(tmp_path / "data")
    descriptor = discover_adapters(_REPOSITORY_ROOT, include_test_adapters=True)[0]
    supervisor = WorkerSupervisor(layout, startup_timeout=10)
    worker = await supervisor.start(descriptor.engine_id, descriptor.launch)
    try:
        yield layout, supervisor, worker
    finally:
        await supervisor.stop_all()


def test_discovery_omits_test_adapters_by_default() -> None:
    descriptors = discover_adapters(_REPOSITORY_ROOT)

    assert [descriptor.engine_id for descriptor in descriptors] == ["openai_compatible", "vieneu"]


def test_discovery_returns_deterministic_isolated_worker_adapters() -> None:
    first = discover_adapters(_REPOSITORY_ROOT, include_test_adapters=True)
    second = discover_adapters(_REPOSITORY_ROOT, include_test_adapters=True)

    assert first == second
    assert [descriptor.engine_id for descriptor in first] == ["fake", "openai_compatible", "vieneu"]
    assert [descriptor.priority for descriptor in first] == [1000, 1000, 1100]
    assert first[0].launch.cwd == _REPOSITORY_ROOT.resolve()
    assert first[0].launch.command == (
        "uv",
        "run",
        "--frozen",
        "--project",
        str((_REPOSITORY_ROOT / "workers" / "fake").resolve()),
        "tts-studio-fake-worker",
    )
    assert first[1].launch.cwd == _REPOSITORY_ROOT.resolve()
    assert first[1].launch.command == (
        "uv",
        "run",
        "--frozen",
        "--project",
        str((_REPOSITORY_ROOT / "workers" / "openai_compatible").resolve()),
        "tts-studio-openai-compatible-worker",
    )
    assert first[2].launch.command == (
        "uv",
        "run",
        "--frozen",
        "--project",
        str((_REPOSITORY_ROOT / "workers" / "vieneu").resolve()),
        "tts-studio-vieneu-worker",
    )


def test_installed_discovery_skips_shadowed_unprovenanced_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shadow = tmp_path / "shadow" / "tts-studio-fake-worker"
    installed = _write_verified_worker_environment(tmp_path / "worker-environment")
    _write_shadow_executable(shadow)
    repository = tmp_path / "empty-repository"
    repository.mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join((str(shadow.parent), str(installed.parent))))

    descriptors = discover_adapters(repository, include_test_adapters=True)

    assert len(descriptors) == 1
    assert descriptors[0].launch.command == (str(installed),)
    assert descriptors[0].launch.cwd == installed.parent.parent


def test_installed_discovery_returns_only_provenanced_allowlisted_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed_fake = _write_verified_worker_environment(tmp_path / "fake-environment")
    installed_vieneu = _write_verified_vieneu_worker_environment(tmp_path / "vieneu-environment")
    shadow = tmp_path / "shadow"
    _write_shadow_executable(shadow / "tts-studio-fake-worker")
    _write_shadow_executable(shadow / "tts-studio-vieneu-worker")
    repository = tmp_path / "empty-repository"
    repository.mkdir()
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join((str(shadow), str(installed_vieneu.parent), str(installed_fake.parent))),
    )

    descriptors = discover_adapters(repository, include_test_adapters=True)

    assert [(item.engine_id, item.priority) for item in descriptors] == [
        ("fake", 1000),
        ("vieneu", 1100),
    ]
    assert descriptors[0].launch.command == (str(installed_fake),)
    assert descriptors[1].launch.command == (str(installed_vieneu),)


def test_active_runtime_generation_takes_precedence_over_source_or_path(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "data"
    generation = runtime_root / "workers" / "fake" / "generations" / "one"
    _write_verified_worker_environment(generation / "venv")
    manifest = generation / "manifest.json"
    manifest.write_text('{"engine_id":"fake","generation":"one"}\n', encoding="utf-8")
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    current = runtime_root / "workers" / "current.json"
    current.write_text(
        '{"engines":{"fake":{"generation":"one","manifest_sha256":"'
        + manifest_hash
        + '"}},"generation":"one","previous_generation":null}\n',
        encoding="utf-8",
    )
    repository = tmp_path / "repository"
    repository.mkdir()

    descriptors = discover_adapters(
        repository,
        include_test_adapters=True,
        runtime_root=runtime_root,
    )

    assert len(descriptors) == 1
    assert descriptors[0].engine_id == "fake"
    assert descriptors[0].launch.command == (
        str(generation / "venv" / "bin" / "tts-studio-fake-worker"),
    )
    assert descriptors[0].launch.cwd == (generation / "venv").resolve()


@pytest.mark.asyncio
async def test_validation_runs_over_authenticated_grpc_without_leaking_secrets_or_paths(
    fake_adapter,
) -> None:
    layout, supervisor, worker = fake_adapter

    description = await worker.stub.Describe(
        engine_pb2.DescribeRequest(),
        metadata=(("x-tts-worker-token", worker.token),),
        timeout=2,
    )
    assert description.engine_id == "fake"
    assert description.engine_version == "0.2.0"
    assert {item.name for item in description.capabilities if item.supported} >= {
        "model_validation",
        "model_download",
        "download_cancellation",
    }

    with pytest.raises(grpc.aio.AioRpcError) as unauthenticated:
        await worker.stub.ValidateModel(
            engine_pb2.ValidateModelRequest(repository_id="fixtures/compatible"),
            timeout=2,
        )
    assert unauthenticated.value.code() == grpc.StatusCode.UNAUTHENTICATED
    assert worker.token not in unauthenticated.value.details()

    response = await supervisor.validate_model(
        "fake",
        engine_pb2.ValidateModelRequest(
            repository_id="fixtures/compatible", requested_revision="main"
        ),
    )

    assert response.repository_id == "fixtures/compatible"
    assert response.requested_revision == "main"
    assert response.resolved_commit == "1c6d281855eeb808859fc335a5ef01f66e82f4a3"
    assert response.compatible is True
    assert response.engine_id == "fake"
    assert response.engine_version == "0.2.0"
    assert response.required_files == ["config.json", "model.bin"]
    assert [(item.id, item.label) for item in response.available_variants] == [
        ("int8", "INT8"),
        ("fp32", "FP32"),
    ]
    assert response.HasField("estimated_bytes")
    assert response.evidence[0].code == "fake_fixture_compatible"
    serialized = response.SerializeToString()
    assert worker.token.encode() not in serialized
    assert str(layout.root).encode() not in serialized

    rejected_input = str(layout.root / "private-model")
    unknown = await supervisor.validate_model(
        "fake",
        engine_pb2.ValidateModelRequest(repository_id=rejected_input),
    )
    assert unknown.compatible is False
    assert unknown.repository_id == ""
    assert unknown.error.code == "model_incompatible"
    assert rejected_input.encode() not in unknown.SerializeToString()


@pytest.mark.asyncio
async def test_download_stream_is_ordered_confined_and_has_verifiable_manifest(
    fake_adapter,
) -> None:
    layout, supervisor, worker = fake_adapter
    destination = layout.staging / "job-compatible"
    destination.mkdir()

    events = [
        event
        async for event in supervisor.download_model(
            "fake",
            engine_pb2.DownloadModelRequest(
                repository_id="fixtures/compatible",
                resolved_commit="1c6d281855eeb808859fc335a5ef01f66e82f4a3",
                variant="int8",
                staging_destination=destination.name,
            ),
        )
    ]

    progress = [event.progress for event in events if event.HasField("progress")]
    assert [item.sequence for item in progress] == [1, 2, 3, 4, 5]
    assert [item.phase for item in progress] == [
        engine_pb2.DOWNLOAD_PHASE_DOWNLOADING,
        engine_pb2.DOWNLOAD_PHASE_DOWNLOADING,
        engine_pb2.DOWNLOAD_PHASE_DOWNLOADING,
        engine_pb2.DOWNLOAD_PHASE_VERIFYING,
        engine_pb2.DOWNLOAD_PHASE_FINALIZING,
    ]
    assert [item.bytes_downloaded for item in progress] == sorted(
        item.bytes_downloaded for item in progress
    )
    manifest = events[-1].manifest
    assert events[-1].WhichOneof("payload") == "manifest"
    assert manifest.byte_size == sum(item.byte_size for item in manifest.files)
    assert {item.relative_path for item in manifest.files} == {"config.json", "model.bin"}
    for item in manifest.files:
        artifact = destination / item.relative_path
        assert artifact.stat().st_size == item.byte_size
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == item.sha256

    encoded_events = b"".join(event.SerializeToString() for event in events)
    assert worker.token.encode() not in encoded_events
    assert str(layout.root).encode() not in encoded_events

    outside = layout.root.parent / "outside"
    outside.mkdir()
    rejected = [
        event
        async for event in supervisor.download_model(
            "fake",
            engine_pb2.DownloadModelRequest(
                repository_id="fixtures/compatible",
                resolved_commit="1c6d281855eeb808859fc335a5ef01f66e82f4a3",
                variant="int8",
                staging_destination=str(outside),
            ),
        )
    ]
    assert len(rejected) == 1
    assert rejected[0].error.code == "download_failed"
    assert str(outside).encode() not in rejected[0].SerializeToString()
    assert tuple(outside.iterdir()) == ()

    checksum_destination = layout.staging / "job-checksum"
    checksum_destination.mkdir()
    checksum_events = [
        event
        async for event in supervisor.download_model(
            "fake",
            engine_pb2.DownloadModelRequest(
                repository_id="fixtures/checksum-failure",
                resolved_commit="d650fc66884d6c22e1988fab5be5e5d529da4704",
                variant="int8",
                staging_destination=checksum_destination.name,
            ),
        )
    ]
    reported = {item.relative_path: item.sha256 for item in checksum_events[-1].manifest.files}
    actual = hashlib.sha256((checksum_destination / "model.bin").read_bytes()).hexdigest()
    assert reported["model.bin"] == "0" * 64
    assert actual != reported["model.bin"]


@pytest.mark.asyncio
async def test_cancelling_a_slow_download_stops_before_artifacts_are_written(
    fake_adapter,
) -> None:
    layout, supervisor, _worker = fake_adapter
    destination = layout.staging / "job-slow"
    destination.mkdir()
    stream = supervisor.download_model(
        "fake",
        engine_pb2.DownloadModelRequest(
            repository_id="fixtures/slow",
            resolved_commit="57f6fdf87f900bf21ae604817ac739186237758f",
            variant="int8",
            staging_destination=destination.name,
        ),
    )

    first = await anext(stream)
    assert first.progress.sequence == 1
    await stream.aclose()
    await asyncio.sleep(0.25)

    assert tuple(destination.iterdir()) == ()
