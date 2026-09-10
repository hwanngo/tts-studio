from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient, Response
from tts_studio_protocol.engine.v1 import engine_pb2

from tts_studio.config import Settings
from tts_studio.models.registry import DownloadState, ModelRegistry
from tts_studio.server.app import create_app
from tts_studio.workers.adapters import AdapterDescriptor
from tts_studio.workers.process import WorkerLaunchSpec
from tts_studio.workers.supervisor import WorkerSupervisor


class RouteSupervisor:
    def __init__(self, response: engine_pb2.ValidateModelResponse | Exception) -> None:
        self.response = response

    async def validate_model(
        self,
        engine_id: str,
        request: engine_pb2.ValidateModelRequest,
        *,
        timeout: float = 10.0,
    ) -> engine_pb2.ValidateModelResponse:
        del engine_id, request, timeout
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def statuses(self) -> tuple[object, ...]:
        return ()

    async def stop_all(self) -> None:
        pass


def _descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        engine_id="fake",
        priority=10,
        launch=WorkerLaunchSpec(command=("fake-worker",), cwd=Path.cwd()),
    )


def _app(tmp_path: Path, response: engine_pb2.ValidateModelResponse | Exception):
    return create_app(
        Settings.resolve(tmp_path / "data"),
        supervisor=cast(WorkerSupervisor, RouteSupervisor(response)),
        adapters=(_descriptor(),),
    )


def _assert_error(response: Response, status: int, code: str) -> dict[str, object]:
    assert response.status_code == status
    correlation_id = response.headers["x-correlation-id"]
    assert str(UUID(correlation_id)) == correlation_id
    error = response.json()["error"]
    assert error["code"] == code
    assert error["correlation_id"] == correlation_id
    return error


def test_model_routes_publish_the_stable_error_envelope_in_openapi(tmp_path: Path) -> None:
    app = _app(tmp_path, RuntimeError("unused"))
    paths = app.openapi()["paths"]

    assert paths["/api/v1/models/validate"]["post"]["responses"]["422"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/ErrorEnvelope"}
    assert paths["/api/v1/models/validate"]["post"]["responses"]["503"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/ErrorEnvelope"}
    assert paths["/api/v1/models/{model_id}"]["get"]["responses"]["404"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/ErrorEnvelope"}


@pytest.mark.asyncio
async def test_validate_route_returns_compatible_adapter_details(tmp_path: Path) -> None:
    app = _app(
        tmp_path,
        engine_pb2.ValidateModelResponse(
            repository_id="fixtures/compatible",
            requested_revision="main",
            resolved_commit="a" * 40,
            compatible=True,
            engine_id="fake",
            engine_version="0.2.0",
            required_files=["config.json", "model.bin"],
            available_variants=[
                engine_pb2.ModelVariant(id="int8", label="INT8"),
                engine_pb2.ModelVariant(id="fp32", label="FP32"),
            ],
            estimated_bytes=512,
            evidence=[
                engine_pb2.CompatibilityEvidence(
                    code="fake_fixture_compatible",
                    message="The fake adapter recognized the fixture",
                )
            ],
        ),
    )

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post(
            "/api/v1/models/validate",
            json={"repository_id": "fixtures/compatible", "requested_revision": "main"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "repository_id": "fixtures/compatible",
        "requested_revision": "main",
        "compatible": True,
        "selected_engine_id": "fake",
        "results": [
            {
                "engine_id": "fake",
                "engine_version": "0.2.0",
                "available": True,
                "compatible": True,
                "resolved_commit": "a" * 40,
                "required_files": ["config.json", "model.bin"],
                "available_variants": [
                    {"id": "int8", "label": "INT8"},
                    {"id": "fp32", "label": "FP32"},
                ],
                "estimated_bytes": 512,
                "evidence": [
                    {
                        "code": "fake_fixture_compatible",
                        "message": "The fake adapter recognized the fixture",
                    }
                ],
                "error_code": None,
                "error_retryable": None,
            }
        ],
    }


@pytest.mark.asyncio
async def test_download_route_preserves_revision_error_code_and_retryability(
    tmp_path: Path,
) -> None:
    app = _app(
        tmp_path,
        engine_pb2.ValidateModelResponse(
            repository_id="fixtures/incompatible",
            compatible=False,
            engine_id="fake",
            engine_version="0.2.0",
            error=engine_pb2.WorkerError(code="revision_not_found", retryable=False),
        ),
    )

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post(
            "/api/v1/downloads", json={"repository_id": "fixtures/incompatible"}
        )

    error = _assert_error(response, 422, "revision_not_found")
    assert error["retryable"] is False


@pytest.mark.asyncio
async def test_download_route_restores_specific_model_incompatible_error_contract(
    tmp_path: Path,
) -> None:
    app = _app(
        tmp_path,
        engine_pb2.ValidateModelResponse(
            repository_id="fixtures/incompatible",
            compatible=False,
            engine_id="fake",
            engine_version="0.2.0",
            error=engine_pb2.WorkerError(code="model_incompatible", retryable=False),
        ),
    )

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post(
            "/api/v1/downloads", json={"repository_id": "fixtures/incompatible"}
        )

    error = _assert_error(response, 422, "model_incompatible")
    assert error["message"] == "No installed engine adapter can run this repository."
    assert error["source"] == "model_registry"
    assert error["retryable"] is False
    assert error["details"] == {}


@pytest.mark.asyncio
async def test_validate_route_distinguishes_incompatible_from_unavailable(tmp_path: Path) -> None:
    incompatible_app = _app(
        tmp_path / "incompatible",
        engine_pb2.ValidateModelResponse(
            repository_id="fixtures/incompatible",
            compatible=False,
            engine_id="fake",
            engine_version="0.2.0",
            evidence=[
                engine_pb2.CompatibilityEvidence(
                    code="fake_fixture_incompatible",
                    message="The fake adapter rejected the fixture",
                )
            ],
            error=engine_pb2.WorkerError(code="model_incompatible", message="not runnable"),
        ),
    )
    unavailable_app = _app(
        tmp_path / "unavailable",
        RuntimeError("/Users/example/private/worker.sock"),
    )

    async with (
        incompatible_app.router.lifespan_context(incompatible_app),
        AsyncClient(
            transport=ASGITransport(app=incompatible_app), base_url="http://test"
        ) as client,
    ):
        incompatible = await client.post(
            "/api/v1/models/validate", json={"repository_id": "fixtures/incompatible"}
        )
    async with (
        unavailable_app.router.lifespan_context(unavailable_app),
        AsyncClient(
            transport=ASGITransport(app=unavailable_app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client,
    ):
        unavailable = await client.post(
            "/api/v1/models/validate", json={"repository_id": "fixtures/compatible"}
        )

    assert incompatible.status_code == 200
    assert incompatible.json()["compatible"] is False
    assert incompatible.json()["results"][0]["error_code"] == "model_incompatible"
    error = _assert_error(unavailable, 503, "adapter_unavailable")
    assert error["retryable"] is True
    assert "/Users/example" not in unavailable.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rejected",
    [
        "",
        "a" * 97,
        "a" * 257,
        "/Users/example/private/repository",
    ],
)
async def test_invalid_repository_id_uses_safe_error_envelope_and_is_not_logged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    rejected: str,
) -> None:
    app = _app(tmp_path, RuntimeError("must not be called"))

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client,
    ):
        response = await client.post(
            "/api/v1/models/validate", json={"repository_id": rejected}
        )

    error = _assert_error(response, 422, "repository_id_invalid")
    assert error["source"] == "model_registry"
    assert error["details"] == {"field": "repository_id"}
    if rejected:
        assert rejected not in response.text
        assert rejected not in caplog.text


def _activate_model(registry: ModelRegistry, machine_path: str) -> None:
    registry.upsert_engine_installation(
        engine_installation_id="fake@1",
        engine_id="fake",
        version="1.0.0",
        command=["uv", "run", "fake-worker"],
        working_directory="workers/fake",
        environment={},
        capabilities={"model_validation": True},
        lifecycle_state="ready",
    )
    job = registry.create_download_job(
        job_id="job-one",
        repository_id="fixtures/compatible",
        requested_revision="main",
        engine_installation_id="fake@1",
        staging_path="staging/job-one",
        correlation_id="correlation-one",
    )
    for state in (
        DownloadState.VALIDATING,
        DownloadState.DOWNLOADING,
        DownloadState.VERIFYING,
        DownloadState.ACTIVATING,
    ):
        registry.transition_download_job(job.id, state)
    registry.activate_model(
        download_job_id=job.id,
        model_id="model-one",
        repository_id="fixtures/compatible",
        requested_revision="main",
        resolved_commit="a" * 40,
        engine_installation_id="fake@1",
        compatibility_evidence={
            "adapter": "fake",
            "message": machine_path,
            "compatible": True,
        },
        runtime_variant="int8",
        manifest={"files": []},
        checksum_summary={"verified": True},
        byte_size=512,
        cache_path="models/fixtures--compatible/aaaaaaaa",
        desired_load_state="unloaded",
        observed_load_state="unloaded",
        replica_summary={"ready": 0},
        last_error={
            "code": "worker_failed",
            "message": machine_path,
            "traceback": f"Traceback from {machine_path}",
        },
    )


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
async def test_model_list_and_detail_return_registry_state_without_machine_paths(
    tmp_path: Path,
    machine_path: str,
) -> None:
    app = _app(tmp_path, RuntimeError("unused"))

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        _activate_model(app.state.model_registry, machine_path)
        listing = await client.get("/api/v1/models")
        detail = await client.get("/api/v1/models/model-one")

    assert listing.status_code == 200
    assert detail.status_code == 200
    assert listing.json() == [detail.json()]
    document = detail.json()
    assert document["repository_id"] == "fixtures/compatible"
    assert document["resolved_commit"] == "a" * 40
    assert document["cache_path"] == "models/fixtures--compatible/aaaaaaaa"
    assert document["runtime_variant"] == "int8"
    assert document["byte_size"] == 512
    assert document["compatibility_evidence"]["message"] == "[redacted]"
    assert document["last_error"] == {
        "code": "worker_failed",
        "message": "[redacted]",
    }
    assert machine_path not in listing.text
    assert machine_path not in detail.text
    assert "Traceback" not in detail.text


@pytest.mark.asyncio
async def test_unknown_model_uses_stable_error_envelope(tmp_path: Path) -> None:
    app = _app(tmp_path, RuntimeError("unused"))

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as client,
    ):
        response = await client.get("/api/v1/models/missing")

    error = _assert_error(response, 404, "model_not_found")
    assert error["source"] == "model_registry"
    assert "missing" not in response.text
