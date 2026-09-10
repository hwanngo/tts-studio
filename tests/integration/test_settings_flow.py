"""Public Settings flows over real Core persistence and fake Worker status."""

import ctypes
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from tts_studio.config import Settings
from tts_studio.generation.domain import GenerationState
from tts_studio.server.app import create_app
from tts_studio.services.lifecycle import LifecycleUnsupportedError
from tts_studio.storage.layout import StorageLayout
from tts_studio.workers.process import WorkerLaunchSpec
from tts_studio.workers.supervisor import WorkerSupervisor

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
def _identity_bound_unlink_available() -> bool:
    try:
        _ = ctypes.CDLL(None).funlinkat
    except (AttributeError, OSError):
        return False
    return True


_FAKE_WORKER_LAUNCH = WorkerLaunchSpec(
    command=("uv", "run", "--project", "workers/fake", "tts-studio-fake-worker"),
    cwd=_REPOSITORY_ROOT,
)


class FakeLifecycle:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    def status(self):
        return SimpleNamespace(
            status="installed", installed=True, running=False, healthy=None, message="ready"
        )

    def install(self):
        self.calls.append("install")
        return SimpleNamespace(operation="install", changed=True, message="installed")

    def uninstall(self):
        self.calls.append("uninstall")
        return SimpleNamespace(operation="uninstall", changed=True, message="uninstalled")

    def restart(self):
        self.calls.append("restart")
        if self.error is not None:
            raise self.error
        return SimpleNamespace(operation="restart", changed=True, message="restarted")


@pytest.mark.asyncio
async def test_settings_public_flow_persists_limits_cleans_artifact_and_reports_worker(
    tmp_path: Path,
) -> None:
    settings = Settings.resolve(tmp_path / "fresh-data")
    supervisor = WorkerSupervisor(StorageLayout.from_root(settings.data_dir), startup_timeout=10)
    worker = await supervisor.start("fake", _FAKE_WORKER_LAUNCH)
    app = create_app(settings, supervisor=supervisor)

    try:
        async with app.router.lifespan_context(app):  # noqa: SIM117
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                defaults = await client.get("/api/v1/settings")
                assert defaults.status_code == 200
                assert defaults.json()["retain_audio_by_default"] is True
                assert defaults.json()["retention"] == {
                    "retained_count": 0,
                    "retained_bytes": 0,
                    "max_age_days": None,
                    "max_storage_bytes": None,
                }

                patched = await client.patch(
                    "/api/v1/settings",
                    json={
                        "retain_audio_by_default": False,
                        "artifact_max_storage_bytes": 100,
                    },
                )
                assert patched.status_code == 200
                assert patched.json()["restart_required"] is False
                assert patched.json()["api_token_env"] is None

                recreated = create_app(Settings.resolve(tmp_path / "fresh-data"), supervisor=supervisor)
                async with AsyncClient(
                    transport=ASGITransport(app=recreated), base_url="http://test"
                ) as recreated_client:
                    persisted = await recreated_client.get("/api/v1/settings")
                assert persisted.status_code == 200
                assert persisted.json()["retain_audio_by_default"] is False
                assert persisted.json()["api_token_env"] is None

                registry = app.state.generation_registry
                artifact_path = app.state.storage_layout.audio / "retained.wav"
                artifact_path.write_bytes(b"123456")
                job = registry.create_job(
                    job_id="settings-retention-job",
                    model_id="model",
                    engine_id="fake",
                    voice_id="voice",
                    text="retained artifact",
                    correlation_id="settings-retention-correlation",
                )
                for state in (
                    GenerationState.LOADING,
                    GenerationState.GENERATING,
                    GenerationState.FINALIZING,
                    GenerationState.COMPLETED,
                ):
                    registry.transition_job(job.id, state)
                registry.create_artifact(
                    job_id=job.id,
                    artifact_id="settings-retained-artifact",
                    path="audio/retained.wav",
                    byte_size=6,
                    sha256="a" * 64,
                    sample_rate=48_000,
                    channel_count=1,
                    frame_count=1,
                )

                before_clear = await client.get("/api/v1/settings")
                assert before_clear.json()["retention"]["retained_count"] == 1
                assert before_clear.json()["retention"]["retained_bytes"] == 6

                invalid = await client.patch(
                    "/api/v1/settings", json={"artifact_max_storage_bytes": 0}
                )
                assert invalid.status_code == 422
                persisted_after_invalid = await client.get("/api/v1/settings")
                assert persisted_after_invalid.json()["artifact_max_storage_bytes"] == 100

                runtime = await client.get("/api/v1/runtime")
                assert runtime.status_code == 200
                assert runtime.json()["workers"] == [
                    {
                        "engine_id": "fake",
                        "status": "ready",
                        "message": "",
                        "capabilities": [
                            "alignment",
                            "download_cancellation",
                            "health",
                            "model_download",
                            "model_lifecycle",
                            "model_validation",
                            "preset_voices",
                            "reference_cloning",
                            "streaming_synthesis",
                            "synthesis_cancellation",
                        ],
                        "engine_version": "0.2.0",
                        "max_concurrency": 1,
                    }
                ]

                cleared = await client.post(
                    "/api/v1/settings/retention/clear", json={"confirm": True}
                )
                assert cleared.status_code == 200
                if _identity_bound_unlink_available():
                    assert cleared.json() == {
                        "deleted": 1,
                        "skipped": 0,
                        "failed": 0,
                        "issues": [],
                    }
                    assert not artifact_path.exists()
                else:
                    assert cleared.json() == {
                        "deleted": 0,
                        "skipped": 1,
                        "failed": 0,
                        "issues": [
                            {
                                "artifact_id": "settings-retained-artifact",
                                "message": "retained artifact was not safely resolvable",
                            }
                        ],
                    }

                summary = (await client.get("/api/v1/settings")).json()["retention"]
                expected_count = 0 if _identity_bound_unlink_available() else 1
                expected_bytes = 0 if _identity_bound_unlink_available() else 6
                assert summary["retained_count"] == expected_count
                assert summary["retained_bytes"] == expected_bytes
    finally:
        assert worker.process.returncode is not None


@pytest.mark.asyncio
async def test_lifecycle_routes_delegate_to_fake_adapter_and_keep_unsupported_stable(
    tmp_path: Path,
) -> None:
    lifecycle = FakeLifecycle(error=LifecycleUnsupportedError("restart is unsupported"))
    app = create_app(Settings.resolve(tmp_path / "data"), lifecycle_adapter=lifecycle)

    async with app.router.lifespan_context(app):  # noqa: SIM117
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            status = await client.get("/api/v1/service")
            missing_confirmation = await client.post(
                "/api/v1/service/install", json={"confirm": False}
            )
            installed = await client.post("/api/v1/service/install", json={"confirm": True})
            unsupported = await client.post("/api/v1/service/restart", json={"confirm": True})

    assert status.status_code == 200
    assert status.json()["status"] == "installed"
    assert missing_confirmation.status_code == 422
    assert installed.status_code == 200
    assert unsupported.status_code == 409
    assert unsupported.json()["error"]["code"] == "service_unsupported"
    assert lifecycle.calls == ["install", "restart"]
