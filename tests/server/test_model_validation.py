from pathlib import Path
from typing import Any, cast

import pytest
from tts_studio_protocol.engine.v1 import engine_pb2

from tts_studio.models.registry import ModelRegistry
from tts_studio.models.service import (
    AdapterUnavailableError,
    InvalidRepositoryIdError,
    ModelService,
)
from tts_studio.storage.db import Database
from tts_studio.storage.layout import StorageLayout
from tts_studio.workers.adapters import AdapterDescriptor
from tts_studio.workers.process import WorkerLaunchSpec


class ValidationSupervisor:
    def __init__(
        self,
        responses: dict[str, engine_pb2.ValidateModelResponse | Exception],
    ) -> None:
        self.responses = responses
        self.requested_engines: list[str] = []

    async def validate_model(
        self,
        engine_id: str,
        request: engine_pb2.ValidateModelRequest,
        *,
        timeout: float = 10.0,
    ) -> engine_pb2.ValidateModelResponse:
        del request, timeout
        self.requested_engines.append(engine_id)
        response = self.responses[engine_id]
        if isinstance(response, Exception):
            raise response
        return response


def _descriptor(engine_id: str, priority: int) -> AdapterDescriptor:
    return AdapterDescriptor(
        engine_id=engine_id,
        priority=priority,
        launch=WorkerLaunchSpec(command=("worker", engine_id), cwd=Path.cwd()),
    )


def _registry(tmp_path: Path) -> ModelRegistry:
    layout = StorageLayout.from_root(tmp_path / "data")
    layout.ensure()
    database = Database(layout.database_path)
    database.migrate()
    return ModelRegistry(database)


def _compatible(engine_id: str, evidence_code: str) -> engine_pb2.ValidateModelResponse:
    return engine_pb2.ValidateModelResponse(
        repository_id="fixtures/compatible",
        resolved_commit="a" * 40,
        compatible=True,
        engine_id=engine_id,
        engine_version="1.0.0",
        required_files=["config.json", "model.bin"],
        available_variants=[engine_pb2.ModelVariant(id="int8", label="INT8")],
        estimated_bytes=512,
        evidence=[
            engine_pb2.CompatibilityEvidence(
                code=evidence_code,
                message=f"{engine_id} recognized the repository",
            )
        ],
    )


@pytest.mark.asyncio
async def test_validation_selects_the_highest_priority_compatible_adapter_and_keeps_evidence(
    tmp_path: Path,
) -> None:
    supervisor = ValidationSupervisor(
        {
            "secondary": _compatible("secondary", "secondary_match"),
            "preferred": _compatible("preferred", "preferred_match"),
        }
    )
    service = ModelService(
        _registry(tmp_path),
        supervisor,
        (_descriptor("secondary", 20), _descriptor("preferred", 10)),
    )

    result = await service.validate("fixtures/compatible", requested_revision="main")

    assert supervisor.requested_engines == ["preferred", "secondary"]
    assert result.repository_id == "fixtures/compatible"
    assert result.requested_revision == "main"
    assert result.compatible is True
    assert result.selected_engine_id == "preferred"
    assert [item.engine_id for item in result.results] == ["preferred", "secondary"]
    assert [item.evidence[0].code for item in result.results] == [
        "preferred_match",
        "secondary_match",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repository_id",
    [
        "",
        " leading/repository",
        "owner/repository ",
        "owner//repository",
        "owner/../repository",
        "owner/repository.git",
        "owner/repo--name",
        "/private/machine/path",
        r"C:\\private\\machine\\path",
    ],
)
async def test_invalid_repository_ids_are_rejected_before_worker_contact(
    tmp_path: Path,
    repository_id: str,
) -> None:
    supervisor = ValidationSupervisor({"fake": _compatible("fake", "match")})
    service = ModelService(_registry(tmp_path), supervisor, (_descriptor("fake", 10),))

    with pytest.raises(InvalidRepositoryIdError, match="repository ID is invalid") as raised:
        await service.validate(repository_id)

    if repository_id:
        assert repository_id not in str(raised.value)
    assert supervisor.requested_engines == []


@pytest.mark.asyncio
async def test_validation_reports_adapter_unavailable_only_when_no_adapter_responds(
    tmp_path: Path,
) -> None:
    machine_path = "/Users/example/private/adapter.sock"
    supervisor = ValidationSupervisor({"fake": RuntimeError(machine_path)})
    service = ModelService(_registry(tmp_path), supervisor, (_descriptor("fake", 10),))

    with pytest.raises(AdapterUnavailableError) as raised:
        await service.validate("fixtures/compatible")

    assert machine_path not in str(raised.value)


@pytest.mark.asyncio
async def test_incompatible_response_is_a_validation_result_with_stable_error_code(
    tmp_path: Path,
) -> None:
    supervisor = ValidationSupervisor(
        {
            "fake": engine_pb2.ValidateModelResponse(
                repository_id="fixtures/incompatible",
                compatible=False,
                engine_id="fake",
                engine_version="1.0.0",
                evidence=[
                    engine_pb2.CompatibilityEvidence(
                        code="architecture_unsupported",
                        message="The model architecture is unsupported",
                    )
                ],
                error=engine_pb2.WorkerError(
                    code="model_incompatible",
                    message="No compatible runtime exists",
                ),
            )
        }
    )
    service = ModelService(_registry(tmp_path), supervisor, (_descriptor("fake", 10),))

    result = await service.validate("fixtures/incompatible")

    assert result.compatible is False
    assert result.selected_engine_id is None
    assert result.results[0].error_code == "model_incompatible"
    assert result.results[0].evidence[0].code == "architecture_unsupported"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "retryable"),
    [("offline", True), ("revision_not_found", False)],
)
async def test_validation_preserves_worker_error_code_and_retryability(
    tmp_path: Path, code: str, retryable: bool
) -> None:
    supervisor = ValidationSupervisor(
        {
            "fake": engine_pb2.ValidateModelResponse(
                repository_id="fixtures/incompatible",
                compatible=False,
                engine_id="fake",
                engine_version="1.0.0",
                error=engine_pb2.WorkerError(code=code, retryable=retryable),
            )
        }
    )
    service = ModelService(_registry(tmp_path), supervisor, (_descriptor("fake", 10),))

    result = await service.validate("fixtures/incompatible")

    assert result.results[0].error_code == code
    assert result.results[0].error_retryable is retryable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "machine_path",
    [
        "failure at:/Users/example/private/model.bin",
        "source=file:///Users/example/private/model.bin",
        r"failure at=C:\Users\example\private\model.bin",
        "socket=unix:/tmp/tts-studio.sock",
        r"source=\\server\private\model.bin",
    ],
)
async def test_worker_compatibility_payload_cannot_expose_a_machine_path(
    tmp_path: Path,
    machine_path: str,
) -> None:
    supervisor = ValidationSupervisor(
        {
            "fake": engine_pb2.ValidateModelResponse(
                repository_id="fixtures/compatible",
                resolved_commit=machine_path,
                compatible=True,
                engine_id="fake",
                engine_version=machine_path,
                required_files=[machine_path, "config.json"],
                available_variants=[engine_pb2.ModelVariant(id="int8", label=machine_path)],
                evidence=[
                    engine_pb2.CompatibilityEvidence(code="recognized", message=machine_path)
                ],
            )
        }
    )
    service = ModelService(_registry(tmp_path), supervisor, (_descriptor("fake", 10),))

    result = await service.validate("fixtures/compatible")

    adapter = result.results[0]
    assert adapter.engine_version == "unknown"
    assert adapter.resolved_commit is None
    assert adapter.required_files == ("config.json",)
    assert adapter.available_variants[0].label == "Adapter variant"
    assert adapter.evidence[0].message == "The adapter supplied compatibility evidence."
    assert machine_path not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_message",
    [
        "See https://user:secret@host/private-model for model details",
        "Authorization: Bearer private-token-value",
        "credential hf_" + "a" * 32,
        "Traceback (most recent call last):\n  adapter.py:12 in validate",
    ],
)
async def test_worker_compatibility_evidence_is_safely_redacted(
    tmp_path: Path, unsafe_message: str
) -> None:
    response = _compatible("fake", "recognized")
    response.evidence[0].message = unsafe_message
    supervisor = ValidationSupervisor({"fake": response})
    service = ModelService(_registry(tmp_path), cast(Any, supervisor), (_descriptor("fake", 10),))

    result = await service.validate("fixtures/compatible")

    assert result.results[0].evidence[0].message == "The adapter supplied compatibility evidence."
    assert unsafe_message not in repr(result)
