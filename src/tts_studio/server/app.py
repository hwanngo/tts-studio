"""FastAPI application factory and lifecycle ownership."""

import logging
import os
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from ipaddress import ip_address
from pathlib import Path

from fastapi import FastAPI

from tts_studio.config import Settings
from tts_studio.events import EventStore
from tts_studio.generation.registry import GenerationRegistry
from tts_studio.generation.service import GenerationService
from tts_studio.models.registry import ModelRegistry
from tts_studio.models.service import ModelService
from tts_studio.providers.registry import ProviderRegistry
from tts_studio.references.service import ReferenceService
from tts_studio.runtime import CoreRunStore
from tts_studio.server.errors import install_error_handlers
from tts_studio.server.events import router as events_router
from tts_studio.server.models import downloads_router
from tts_studio.server.models import router as models_router
from tts_studio.server.routes.generation import router as generation_router
from tts_studio.server.routes.openai import router as openai_router
from tts_studio.server.routes.providers import router as providers_router
from tts_studio.server.routes.references import router as references_router
from tts_studio.server.routes.runtime import router as runtime_router
from tts_studio.server.routes.service import router as service_router
from tts_studio.server.routes.settings import router as settings_router
from tts_studio.server.routes.system import router as system_router
from tts_studio.services import LifecycleAdapter, ServiceDefinition, ServiceManager, ServicePlatform
from tts_studio.settings.repository import CoreSettingsRepository
from tts_studio.settings.service import SettingsService
from tts_studio.storage.db import Database
from tts_studio.storage.layout import StorageLayout
from tts_studio.voices.registry import SavedVoiceRegistry
from tts_studio.voices.service import SavedVoiceService
from tts_studio.web_assets import mount_web_app
from tts_studio.workers.adapters import AdapterDescriptor, discover_adapters
from tts_studio.workers.supervisor import WorkerSupervisor

_LOGGER = logging.getLogger(__name__)


def create_app(
    settings: Settings,
    supervisor: WorkerSupervisor | None = None,
    static_root: Path | None = None,
    adapters: Sequence[AdapterDescriptor] | None = None,
    *,
    include_test_adapters: bool = False,
    lifecycle_adapter: object | None = None,
) -> FastAPI:
    """Compose the Core application around its resolved runtime dependencies."""
    layout = StorageLayout.from_root(settings.data_dir)
    layout.ensure()
    active_supervisor = supervisor or WorkerSupervisor(layout, startup_timeout=10.0)
    database = Database(layout.database_path)
    settings_repository = CoreSettingsRepository(database)
    registry = ModelRegistry(database)
    generation_registry = GenerationRegistry(database)
    reference_service = ReferenceService(database, layout)
    saved_voice_service = SavedVoiceService(SavedVoiceRegistry(database), reference_service, layout)
    provider_registry = ProviderRegistry(database)
    event_store = EventStore(database)
    active_adapters = tuple(
        sorted(
            adapters
            if adapters is not None
            else discover_adapters(
                _repository_root(),
                include_test_adapters=include_test_adapters,
                runtime_root=settings.data_dir,
            ),
            key=lambda item: (item.priority, item.engine_id),
        )
    )
    manage_adapters = supervisor is None
    model_service = ModelService(
        registry,
        active_supervisor,
        active_adapters,
        layout=layout,
        event_store=event_store,
        generation_registry=generation_registry,
    )
    generation_service = GenerationService(
        generation_registry,
        registry,
        active_supervisor,
        layout=layout,
        event_store=event_store,
        reference_service=reference_service,
        saved_voice_service=saved_voice_service,
        provider_registry=provider_registry,
    )
    settings_service = SettingsService(settings_repository, generation_service)
    generation_service.set_retention_default_provider(
        lambda: settings_service.apply_generation_defaults()
    )
    active_lifecycle_adapter = lifecycle_adapter or _default_lifecycle_adapter(settings, layout)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        database.migrate()
        # Generation recovery owns its private PCM staging files. Run it first
        # so model recovery only sees download staging directories, not Core's
        # dot-prefixed generation temporaries.
        await generation_service.recover(resume_alignments=False)
        reference_service.recover()
        model_service.recover_downloads()
        if manage_adapters:
            for descriptor in active_adapters:
                try:
                    await active_supervisor.start(descriptor.engine_id, descriptor.launch)
                    desired = max(
                        (
                            model.desired_replicas
                            for model in registry.list_models()
                            if model.engine_installation_id.split("@", maxsplit=1)[0]
                            == descriptor.engine_id
                        ),
                        default=1,
                    )
                    if desired > 1:
                        await active_supervisor.ensure_replicas(
                            registry.get_model(
                                next(
                                    model.id
                                    for model in registry.list_models()
                                    if model.engine_installation_id.split("@", maxsplit=1)[0]
                                    == descriptor.engine_id
                                )
                            ),
                            desired,
                        )
                except Exception as error:  # noqa: BLE001
                    _LOGGER.warning(
                        "engine_startup_failed engine_id=%s error_type=%s",
                        descriptor.engine_id,
                        type(error).__name__,
                    )
        generation_service.resume_alignments()
        try:
            yield
        finally:
            await generation_service.close()
            await model_service.close()
            await active_supervisor.stop_all()

    app = FastAPI(lifespan=lifespan)
    api_token = _resolve_api_token(settings.api_token_env)
    if api_token is None and not _is_loopback_host(settings.host):
        raise ValueError("non-loopback Core requires a configured API token")
    install_error_handlers(app, api_token=api_token)
    app.state.settings = settings
    app.state.storage_layout = layout
    app.state.supervisor = active_supervisor
    app.state.settings_repository = settings_repository
    app.state.settings_service = settings_service
    app.state.lifecycle_adapter = active_lifecycle_adapter
    app.state.model_registry = registry
    app.state.generation_registry = generation_registry
    app.state.generation_service = generation_service
    app.state.reference_service = reference_service
    app.state.saved_voice_service = saved_voice_service
    app.state.provider_registry = provider_registry
    app.state.model_service = model_service
    app.state.event_store = event_store
    app.include_router(system_router)
    app.include_router(settings_router)
    app.include_router(runtime_router)
    app.include_router(service_router)
    app.include_router(models_router)
    app.include_router(downloads_router)
    app.include_router(generation_router)
    app.include_router(openai_router)
    app.include_router(providers_router)
    app.include_router(references_router)
    app.include_router(events_router)
    mount_web_app(app, static_root)
    return app


def _repository_root() -> Path:
    """Return the source checkout root when adapters are shipped beside Core."""
    return Path(__file__).resolve().parents[3]


def _default_lifecycle_adapter(settings: Settings, layout: StorageLayout) -> object | None:
    platform_name = "windows" if os.name == "nt" else sys.platform
    aliases = {"macos": "darwin", "win32": "windows"}
    try:
        platform = ServicePlatform(aliases.get(platform_name, platform_name))
        definition = ServiceDefinition(
            executable=Path(sys.argv[0]).expanduser().resolve(),
            data_dir=layout.root,
            host=settings.host,
            port=settings.port,
            name="tts-studio",
        )
        manager = ServiceManager(definition, platform=platform, home=Path.home().resolve())
    except OSError, ValueError:
        return None
    return LifecycleAdapter(manager, CoreRunStore(layout), in_process=True)


def _resolve_api_token(api_token_env: str | None) -> str | None:
    if api_token_env is None:
        return None
    value = os.environ.get(api_token_env)
    if not value:
        raise ValueError("configured API token environment variable is not set")
    return value


def _is_loopback_host(host: str) -> bool:
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False
