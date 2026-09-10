"""Commands for interacting with a local TTS Studio Core."""

from __future__ import annotations

import ipaddress
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, NoReturn

import typer
import uvicorn
from pydantic import BaseModel

from tts_studio.client import (
    CoreApiError,
    CoreClient,
    CoreUnavailable,
)
from tts_studio.config import Settings
from tts_studio.generated.api import (
    AudioArtifactResponse,
    DownloadJobResponse,
    ErrorBody,
    ErrorEnvelope,
    GenerationJobResponse,
    ModelInstallationResponse,
    ModelValidationResponse,
    SystemStatus,
)
from tts_studio.runtime import CoreRunRecord, CoreRunStore, core_health_url
from tts_studio.runtime_upgrade import (
    RuntimeUpgradeError,
    RuntimeUpgradeManager,
)
from tts_studio.server.app import create_app
from tts_studio.services import ServiceDefinition, ServiceManager, ServicePlatform
from tts_studio.storage.layout import StorageLayout

app = typer.Typer(no_args_is_help=True)
models_app = typer.Typer(no_args_is_help=True)
voices_app = typer.Typer(no_args_is_help=True)
providers_app = typer.Typer(no_args_is_help=True)
jobs_app = typer.Typer(no_args_is_help=True)
history_app = typer.Typer(no_args_is_help=True)
service_app = typer.Typer(no_args_is_help=True)
runtime_app = typer.Typer(no_args_is_help=True)
app.add_typer(models_app, name="models", help="Validate, download, and manage models.")
app.add_typer(voices_app, name="voices", help="Inspect runtime preset Voices.")
app.add_typer(providers_app, name="providers", help="Manage remote speech providers.")
app.add_typer(jobs_app, name="jobs", help="Inspect and cancel Generation Jobs.")
app.add_typer(history_app, name="history", help="List and delete retained audio history.")
app.add_typer(service_app, name="service", help="Install and inspect a per-user Core service.")
app.add_typer(runtime_app, name="runtime", help="Manage installed Worker runtimes.")

_DEFAULT_CORE_URL = "http://127.0.0.1:7860"
_RUNTIME_ENGINES = ("openai_compatible", "vieneu")
_TERMINAL_DOWNLOAD_STATES = {"completed", "cancelled", "failed"}
_TERMINAL_GENERATION_STATES = {"completed", "cancelled", "failed"}


@app.command()
def serve(
    host: Annotated[str | None, typer.Option()] = None,
    port: Annotated[int | None, typer.Option()] = None,
    data_dir: Annotated[Path | None, typer.Option()] = None,
    background: Annotated[bool, typer.Option("--background")] = False,
    background_child: Annotated[bool, typer.Option("--background-child", hidden=True)] = False,
    claim_fd: Annotated[int | None, typer.Option("--claim-fd", hidden=True)] = None,
    claim_token: Annotated[str | None, typer.Option("--claim-token", hidden=True)] = None,
    include_test_adapters: Annotated[
        bool,
        typer.Option(
            "--include-test-adapters",
            help="Include the deterministic fake Worker for test environments.",
        ),
    ] = False,
) -> None:
    """Run the Core in the foreground."""
    settings = Settings.resolve(data_dir)
    if host is not None:
        settings = settings.model_copy(update={"host": host})
    if port is not None:
        settings = settings.model_copy(update={"port": port})
    if not _is_loopback_host(settings.host):
        token_env = settings.api_token_env
        if token_env is None or not os.environ.get(token_env):
            raise typer.BadParameter(
                "Phase 1 accepts only loopback IP literals unless configured bearer "
                "authentication is available for non-loopback serving",
                param_hint="--host",
            )

    if background:
        _start_background(settings, include_test_adapters)
        return

    run_store = CoreRunStore(StorageLayout.from_root(settings.data_dir))
    if not (background_child and claim_fd is not None):
        existing = run_store.reconcile()
        if existing is not None:
            raise typer.BadParameter(
                f"Core is already running with PID {existing.pid} using {existing.data_dir}."
            )
    try:
        claim = (
            run_store.adopt_claim(claim_fd, claim_token or "")
            if background_child and claim_fd is not None
            else run_store.claim()
        )
    except (OSError, RuntimeError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    try:
        run_store.write(
            CoreRunRecord(
                pid=os.getpid(),
                host=settings.host,
                port=settings.port,
                data_dir=str(settings.data_dir),
                started_at=datetime.now(UTC).isoformat(),
                owner_token=claim.token,
            )
        )
        application = (
            create_app(settings, include_test_adapters=True)
            if include_test_adapters
            else create_app(settings)
        )
        uvicorn.run(application, host=settings.host, port=settings.port)
    finally:
        try:
            run_store.clear_if_owner(os.getpid(), claim.token, claim)
        finally:
            claim.release()


@app.command()
def status(
    url: Annotated[str, typer.Option()] = _DEFAULT_CORE_URL,
) -> None:
    """Print the public status of a running Core."""
    try:
        system = CoreClient(url).system_status()
    except CoreUnavailable:
        typer.echo(f"Unable to reach Core at {url}; start it with 'tts serve'.", err=True)
        raise typer.Exit(code=3) from None

    _print_system_status(system)


@app.command()
def stop(
    data_dir: Annotated[Path | None, typer.Option()] = None,
    timeout: Annotated[float, typer.Option(min=0.0)] = 10.0,
) -> None:
    """Stop a detached Core using its managed run record."""
    store = CoreRunStore(StorageLayout.from_root(Settings.resolve(data_dir).data_dir))
    record = store.reconcile()
    if record is None:
        typer.echo("Core is not running.")
        return
    _stop_record(store, record, timeout)
    typer.echo(f"Stopped Core process {record.pid}.")


@app.command()
def restart(
    data_dir: Annotated[Path | None, typer.Option()] = None,
    timeout: Annotated[float, typer.Option(min=0.0)] = 10.0,
    include_test_adapters: Annotated[
        bool,
        typer.Option("--include-test-adapters", help="Include the deterministic fake Worker."),
    ] = False,
) -> None:
    """Restart a detached Core using the resolved settings."""
    settings = Settings.resolve(data_dir)
    store = CoreRunStore(StorageLayout.from_root(settings.data_dir))
    record = store.reconcile()
    if record is not None:
        _stop_record(store, record, timeout)
    _start_background(settings, include_test_adapters)


@app.command()
def logs(
    data_dir: Annotated[Path | None, typer.Option()] = None,
    lines: Annotated[int, typer.Option(min=1, max=10_000)] = 100,
) -> None:
    """Print recent detached Core logs."""
    store = CoreRunStore(StorageLayout.from_root(Settings.resolve(data_dir).data_dir))
    output = store.tail_logs(lines)
    if output:
        typer.echo(output, nl=not output.endswith("\n"))
    else:
        typer.echo("No Core logs available.")


@service_app.command("install")
def install_service(
    data_dir: Annotated[Path | None, typer.Option()] = None,
    platform: Annotated[str | None, typer.Option("--platform")] = None,
) -> None:
    """Install and start the per-user Core login service."""
    settings = Settings.resolve(data_dir)
    record = CoreRunStore(StorageLayout.from_root(settings.data_dir)).reconcile()
    if record is not None:
        raise typer.BadParameter(
            f"Core is already running with PID {record.pid} using {record.data_dir}."
        )
    manager = _service_manager(settings, _resolve_service_platform(platform))
    try:
        paths = manager.install()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise typer.BadParameter(f"could not install Core service: {error}") from error
    target = str(paths.definition) if paths.definition is not None else "Task Scheduler"
    typer.echo(f"Installed Core service ({manager.platform.value}) at {target}.")


@service_app.command("uninstall")
def uninstall_service(
    data_dir: Annotated[Path | None, typer.Option()] = None,
    platform: Annotated[str | None, typer.Option("--platform")] = None,
) -> None:
    """Stop and remove the per-user Core login service."""
    settings = Settings.resolve(data_dir)
    manager = _service_manager(settings, _resolve_service_platform(platform))
    store = CoreRunStore(StorageLayout.from_root(settings.data_dir))
    record = store.reconcile()
    if record is not None:
        _stop_record(store, record, timeout=10.0)
    try:
        removed = manager.uninstall()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise typer.BadParameter(f"could not uninstall Core service: {error}") from error
    if removed:
        typer.echo(f"Uninstalled Core service ({manager.platform.value}).")
    else:
        typer.echo("Core service is not installed.")


@service_app.command("status")
def service_status(data_dir: Annotated[Path | None, typer.Option()] = None) -> None:
    """Show the managed Core service's run-record and public health state."""
    settings = Settings.resolve(data_dir)
    store = CoreRunStore(StorageLayout.from_root(settings.data_dir))
    record = store.reconcile()
    if record is None:
        typer.echo("Core service is not running.")
        return
    url = _core_url(record.host, record.port)
    try:
        system = CoreClient(url).system_status()
    except CoreUnavailable:
        typer.echo(f"Core service record found, but Core is unavailable at {url}.", err=True)
        raise typer.Exit(code=3) from None
    typer.echo(f"Core service is healthy at {url}.")
    _print_system_status(system)


@runtime_app.command("upgrade")
def upgrade_runtime(
    artifact_dir: Annotated[Path, typer.Option("--artifact-dir")],
    generation: Annotated[str | None, typer.Option("--generation")] = None,
    data_dir: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Install and activate a verified Worker runtime generation."""
    settings = Settings.resolve(data_dir)
    manager = _runtime_manager(settings)
    generation_id = generation or _runtime_generation_id()
    store = CoreRunStore(StorageLayout.from_root(settings.data_dir))
    record = store.reconcile()
    if record is not None:
        _stop_record(store, record, timeout=10.0)
    try:
        result = manager.upgrade(artifact_dir, generation_id)
    except (OSError, ValueError, RuntimeUpgradeError) as error:
        _restart_after_runtime_failure(settings, record)
        raise typer.BadParameter(f"could not upgrade Worker runtime: {error}") from error
    if record is not None:
        try:
            _start_background(settings, include_test_adapters=False)
        except (OSError, RuntimeError, typer.BadParameter) as error:
            _rollback_after_restart_failure(settings, manager)
            raise typer.BadParameter(
                f"runtime generation {result.generation_id} activated but Core restart failed: {error}"
            ) from error
    typer.echo(f"Activated Worker runtime generation {result.generation_id}.")
    if record is not None:
        typer.echo("Core restarted successfully.")


@runtime_app.command("rollback")
def rollback_runtime(data_dir: Annotated[Path | None, typer.Option()] = None) -> None:
    """Restore the previous verified Worker runtime generation."""
    settings = Settings.resolve(data_dir)
    manager = _runtime_manager(settings)
    store = CoreRunStore(StorageLayout.from_root(settings.data_dir))
    record = store.reconcile()
    if record is not None:
        _stop_record(store, record, timeout=10.0)
    try:
        result = manager.rollback()
    except (OSError, ValueError, RuntimeUpgradeError) as error:
        _restart_after_runtime_failure(settings, record)
        raise typer.BadParameter(f"could not roll back Worker runtime: {error}") from error
    if record is not None:
        try:
            _start_background(settings, include_test_adapters=False)
        except (OSError, RuntimeError, typer.BadParameter) as error:
            raise typer.BadParameter(f"runtime rollback activated but Core restart failed: {error}") from error
    old_generation = next(
        (value for value in result.previous_generations.values() if value is not None),
        "unknown",
    )
    typer.echo(f"Rolled back Worker runtime from {old_generation} to {result.generation_id}.")
    if record is not None:
        typer.echo("Core restarted successfully.")


@runtime_app.command("status")
def runtime_status(
    data_dir: Annotated[Path | None, typer.Option()] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show active and previous Worker runtime generations."""
    try:
        status = _runtime_manager(Settings.resolve(data_dir)).status()
    except (OSError, ValueError, RuntimeUpgradeError) as error:
        raise typer.BadParameter(f"could not read Worker runtime status: {error}") from error
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "active_generations": status.active_generations,
                    "previous_generations": status.previous_generations,
                    "state": status.state,
                },
                sort_keys=True,
            )
        )
        return
    typer.echo(f"Runtime state: {status.state}")
    for engine, generation in status.active_generations.items():
        previous = status.previous_generations[engine]
        typer.echo(f"Worker {engine}: active={generation or 'none'} previous={previous or 'none'}")


@models_app.command("validate")
def validate_model(
    repository_id: Annotated[str, typer.Argument()],
    revision: Annotated[str | None, typer.Option()] = None,
    url: Annotated[str, typer.Option()] = _DEFAULT_CORE_URL,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check whether installed adapters can run a Hugging Face repository."""
    client = CoreClient(url)
    try:
        result = client.validate_model(repository_id, revision)
    except CoreUnavailable:
        _exit_core_unavailable(url, json_output=json_output)
    except CoreApiError as error:
        _exit_api_error(error, json_output=json_output)

    if json_output:
        _print_json(result)
    else:
        _print_validation(result)


@models_app.command("list")
def list_models(
    url: Annotated[str, typer.Option()] = _DEFAULT_CORE_URL,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List active Model Installations."""
    client = CoreClient(url)
    try:
        installations = client.list_models()
    except CoreUnavailable:
        _exit_core_unavailable(url, json_output=json_output)
    except CoreApiError as error:
        _exit_api_error(error, json_output=json_output)

    if json_output:
        typer.echo(
            json.dumps(
                [item.model_dump(mode="json") for item in installations],
                sort_keys=True,
            )
        )
        return
    if not installations:
        typer.echo("No models installed.")
        return
    for index, installation in enumerate(installations):
        if index:
            typer.echo()
        _print_model(installation)


@models_app.command("download")
def download_model(
    repository_id: Annotated[str, typer.Argument()],
    revision: Annotated[str | None, typer.Option()] = None,
    variant: Annotated[str | None, typer.Option()] = None,
    url: Annotated[str, typer.Option()] = _DEFAULT_CORE_URL,
    wait: Annotated[bool, typer.Option("--wait/--no-wait")] = True,
    poll_interval: Annotated[float, typer.Option(min=0.0)] = 0.25,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Start a model download and, by default, wait for its terminal state."""
    client = CoreClient(url)
    try:
        job = client.start_download(
            repository_id,
            requested_revision=revision,
            variant=variant,
        )
        if not json_output:
            typer.echo(f"Job: {job.id}")
            _print_download_progress(job)
        while wait and job.state not in _TERMINAL_DOWNLOAD_STATES:
            time.sleep(poll_interval)
            job = client.get_download(job.id)
            if not json_output:
                _print_download_progress(job)
    except CoreUnavailable:
        _exit_core_unavailable(url, json_output=json_output)
    except CoreApiError as error:
        _exit_api_error(error, json_output=json_output)

    if json_output:
        _print_json(job)
    elif job.state == "completed" and job.target_model_id is not None:
        typer.echo(f"Model: {job.target_model_id}")

    if job.state in {"cancelled", "failed"}:
        _exit_terminal_download(job, json_output=json_output)


@models_app.command("cancel")
def cancel_download(
    download_id: Annotated[str, typer.Argument()],
    url: Annotated[str, typer.Option()] = _DEFAULT_CORE_URL,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Request cancellation of a Download Job."""
    try:
        job = CoreClient(url).cancel_download(download_id)
    except CoreUnavailable:
        _exit_core_unavailable(url, json_output=json_output)
    except CoreApiError as error:
        _exit_api_error(error, json_output=json_output)

    if json_output:
        _print_json(job)
    else:
        typer.echo(f"Cancellation requested for {job.id}.")


@models_app.command("remove")
def remove_model(
    model_id: Annotated[str, typer.Argument()],
    url: Annotated[str, typer.Option()] = _DEFAULT_CORE_URL,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Unload and remove a Model Installation."""
    try:
        CoreClient(url).remove_model(model_id)
    except CoreUnavailable:
        _exit_core_unavailable(url, json_output=json_output)
    except CoreApiError as error:
        _exit_api_error(error, json_output=json_output)

    if json_output:
        typer.echo(json.dumps({"id": model_id, "removed": True}, sort_keys=True))
    else:
        typer.echo(f"Removed {model_id}.")


@voices_app.command("list")
def list_voices(
    model_id: Annotated[str, typer.Option("--model")],
    saved: Annotated[bool, typer.Option("--saved")] = False,
    url: Annotated[str, typer.Option()] = _DEFAULT_CORE_URL,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List runtime preset Voices reported by the selected Worker."""
    try:
        client = CoreClient(url)
        voices = client.list_saved_voices(model_id) if saved else client.list_voices(model_id)
    except CoreUnavailable:
        _exit_core_unavailable(url, json_output=json_output)
    except CoreApiError as error:
        _exit_api_error(error, json_output=json_output)

    if json_output:
        _print_json(voices)
    elif not voices:
        typer.echo("No runtime Voices available.")
    else:
        for voice in voices:
            typer.echo(f"{voice.id}: {voice.label}")


@providers_app.command("list")
def list_providers(
    url: Annotated[str, typer.Option()] = _DEFAULT_CORE_URL,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List configured remote provider profiles."""
    try:
        providers = CoreClient(url).list_providers()
    except CoreUnavailable:
        _exit_core_unavailable(url, json_output=json_output)
    except CoreApiError as error:
        _exit_api_error(error, json_output=json_output)
    if json_output:
        _print_json(providers)
    elif not providers:
        typer.echo("No remote providers configured.")
    else:
        for provider in providers:
            typer.echo(f"{provider.id}: {provider.label} ({provider.base_url}, {provider.model})")


@providers_app.command("create")
def create_provider(
    label: Annotated[str, typer.Option("--label")],
    base_url: Annotated[str, typer.Option("--base-url")],
    model: Annotated[str, typer.Option("--model")],
    api_key_env: Annotated[str, typer.Option("--api-key-env")],
    url: Annotated[str, typer.Option()] = _DEFAULT_CORE_URL,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Create an OpenAI-compatible provider profile."""
    try:
        provider = CoreClient(url).create_provider(label=label, base_url=base_url, model=model, api_key_env=api_key_env)
    except CoreUnavailable:
        _exit_core_unavailable(url, json_output=json_output)
    except CoreApiError as error:
        _exit_api_error(error, json_output=json_output)
    if json_output:
        _print_json(provider)
    else:
        typer.echo(f"Created provider {provider.id} ({provider.label}).")


@providers_app.command("update")
def update_provider(
    provider_id: Annotated[str, typer.Argument()],
    label: Annotated[str, typer.Option("--label")],
    base_url: Annotated[str, typer.Option("--base-url")],
    model: Annotated[str, typer.Option("--model")],
    api_key_env: Annotated[str, typer.Option("--api-key-env")],
    url: Annotated[str, typer.Option()] = _DEFAULT_CORE_URL,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Update an OpenAI-compatible provider profile."""
    try:
        provider = CoreClient(url).update_provider(
            provider_id,
            label=label,
            base_url=base_url,
            model=model,
            api_key_env=api_key_env,
        )
    except CoreUnavailable:
        _exit_core_unavailable(url, json_output=json_output)
    except CoreApiError as error:
        _exit_api_error(error, json_output=json_output)
    if json_output:
        _print_json(provider)
    else:
        typer.echo(f"Updated provider {provider.id} ({provider.label}).")


@providers_app.command("validate")
def validate_provider(
    provider_id: Annotated[str, typer.Argument()],
    url: Annotated[str, typer.Option()] = _DEFAULT_CORE_URL,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Validate a provider profile's configured credential."""
    try:
        provider = CoreClient(url).validate_provider(provider_id)
    except CoreUnavailable:
        _exit_core_unavailable(url, json_output=json_output)
    except CoreApiError as error:
        _exit_api_error(error, json_output=json_output)
    if json_output:
        _print_json(provider)
    else:
        typer.echo(f"Provider {provider.label} is configured.")


@providers_app.command("delete")
def delete_provider(
    provider_id: Annotated[str, typer.Argument()],
    url: Annotated[str, typer.Option()] = _DEFAULT_CORE_URL,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Delete a remote provider profile."""
    try:
        CoreClient(url).delete_provider(provider_id)
    except CoreUnavailable:
        _exit_core_unavailable(url, json_output=json_output)
    except CoreApiError as error:
        _exit_api_error(error, json_output=json_output)
    if json_output:
        typer.echo(json.dumps({"id": provider_id, "deleted": True}, sort_keys=True))
    else:
        typer.echo(f"Deleted provider {provider_id}.")


@app.command()
def speak(
    model_id: Annotated[str, typer.Option("--model")],
    voice_id: Annotated[str, typer.Option("--voice")],
    output: Annotated[Path, typer.Option("--output")],
    text: Annotated[str | None, typer.Argument()] = None,
    file: Annotated[Path | None, typer.Option("--file")] = None,
    url: Annotated[str, typer.Option()] = _DEFAULT_CORE_URL,
    poll_interval: Annotated[float, typer.Option(min=0.0)] = 0.25,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Generate finalized WAV audio through the Core."""
    if (text is None) == (file is None):
        raise typer.BadParameter("provide exactly one of TEXT or --file")
    if file is not None:
        try:
            text = file.read_text(encoding="utf-8")
        except OSError as error:
            raise typer.BadParameter(f"could not read {file}: {error}", param_hint="--file") from error
    assert text is not None

    client = CoreClient(url)
    try:
        job = client.create_generation(
            model_id,
            voice_id,
            text,
            retain_artifact=True,
        )
        while job.state not in _TERMINAL_GENERATION_STATES:
            time.sleep(poll_interval)
            job = client.get_generation(job.id)
        if job.state != "completed":
            _exit_terminal_generation(job, json_output=json_output)
        if job.artifact_id is None:
            _exit_generation_cli_error(
                "generation_artifact_missing",
                "The completed Generation Job has no retained Audio Artifact.",
                job.correlation_id,
                json_output=json_output,
            )
        try:
            output.write_bytes(client.download_artifact(job.artifact_id))
        except OSError as error:
            _exit_generation_cli_error(
                "output_write_failed",
                f"Could not write the finalized WAV to {output}: {error}",
                job.correlation_id,
                json_output=json_output,
            )
    except CoreUnavailable:
        _exit_core_unavailable(url, json_output=json_output)
    except CoreApiError as error:
        _exit_api_error(error, json_output=json_output)

    if json_output:
        _print_json(job)
    else:
        typer.echo(f"Wrote {output}.")


@jobs_app.command("list")
def list_jobs(
    url: Annotated[str, typer.Option()] = _DEFAULT_CORE_URL,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List Generation Jobs."""
    try:
        jobs = CoreClient(url).list_generations()
    except CoreUnavailable:
        _exit_core_unavailable(url, json_output=json_output)
    except CoreApiError as error:
        _exit_api_error(error, json_output=json_output)
    if json_output:
        _print_json(jobs)
    elif not jobs:
        typer.echo("No Generation Jobs.")
    else:
        for job in jobs:
            _print_generation(job)


@jobs_app.command("get")
def get_job(
    job_id: Annotated[str, typer.Argument()],
    url: Annotated[str, typer.Option()] = _DEFAULT_CORE_URL,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Read one Generation Job."""
    try:
        job = CoreClient(url).get_generation(job_id)
    except CoreUnavailable:
        _exit_core_unavailable(url, json_output=json_output)
    except CoreApiError as error:
        _exit_api_error(error, json_output=json_output)
    if json_output:
        _print_json(job)
    else:
        _print_generation(job)


@jobs_app.command("cancel")
def cancel_job(
    job_id: Annotated[str, typer.Argument()],
    url: Annotated[str, typer.Option()] = _DEFAULT_CORE_URL,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Request cancellation of a Generation Job."""
    try:
        job = CoreClient(url).cancel_generation(job_id)
    except CoreUnavailable:
        _exit_core_unavailable(url, json_output=json_output)
    except CoreApiError as error:
        _exit_api_error(error, json_output=json_output)
    if json_output:
        _print_json(job)
    else:
        typer.echo(f"Cancellation requested for {job.id}.")


@history_app.command("list")
def list_history(
    url: Annotated[str, typer.Option()] = _DEFAULT_CORE_URL,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List retained Audio Artifacts."""
    try:
        history = CoreClient(url).list_history()
    except CoreUnavailable:
        _exit_core_unavailable(url, json_output=json_output)
    except CoreApiError as error:
        _exit_api_error(error, json_output=json_output)
    if json_output:
        _print_json(history)
    elif not history:
        typer.echo("History is empty.")
    else:
        for artifact in history:
            _print_artifact(artifact)


@history_app.command("delete")
def delete_history(
    artifact_id: Annotated[str, typer.Argument()],
    url: Annotated[str, typer.Option()] = _DEFAULT_CORE_URL,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Delete one retained Audio Artifact from History."""
    try:
        CoreClient(url).delete_history(artifact_id)
    except CoreUnavailable:
        _exit_core_unavailable(url, json_output=json_output)
    except CoreApiError as error:
        _exit_api_error(error, json_output=json_output)
    if json_output:
        typer.echo(json.dumps({"id": artifact_id, "deleted": True}, sort_keys=True))
    else:
        typer.echo(f"Deleted {artifact_id}.")


def _is_loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _resolve_service_platform(value: str | None) -> ServicePlatform:
    if value is None:
        value = "windows" if os.name == "nt" else sys.platform
    normalized = value.lower()
    aliases = {"macos": "darwin", "win32": "windows"}
    try:
        return ServicePlatform(aliases.get(normalized, normalized))
    except ValueError as error:
        raise typer.BadParameter(
            "platform must be one of: darwin, linux, windows", param_hint="--platform"
        ) from error


def _service_manager(settings: Settings, platform: str | ServicePlatform) -> ServiceManager:
    executable = Path(sys.argv[0]).expanduser().resolve()
    definition = ServiceDefinition(
        executable=executable,
        data_dir=settings.data_dir,
        host=settings.host,
        port=settings.port,
        name="tts-studio",
    )
    return ServiceManager(
        definition,
        platform=(
            platform
            if isinstance(platform, ServicePlatform)
            else _resolve_service_platform(platform)
        ),
        home=Path.home().resolve(),
    )


def _runtime_manager(settings: Settings) -> RuntimeUpgradeManager:
    return RuntimeUpgradeManager(
        StorageLayout.from_root(settings.data_dir),
        required_engines=_RUNTIME_ENGINES,
    )


def _runtime_generation_id() -> str:
    return datetime.now(UTC).strftime("runtime-%Y%m%dT%H%M%S%fZ")


def _restart_after_runtime_failure(settings: Settings, record: CoreRunRecord | None) -> None:
    if record is None:
        return
    try:
        _start_background(settings, include_test_adapters=False)
    except (OSError, RuntimeError, typer.BadParameter):
        pass


def _rollback_after_restart_failure(
    settings: Settings, manager: RuntimeUpgradeManager
) -> None:
    try:
        manager.rollback()
        _start_background(settings, include_test_adapters=False)
    except (OSError, RuntimeError, ValueError, RuntimeUpgradeError, typer.BadParameter):
        pass


def _core_url(host: str, port: int) -> str:
    return core_health_url(host, port)


def _start_background(settings: Settings, include_test_adapters: bool) -> None:
    layout = StorageLayout.from_root(settings.data_dir)
    layout.ensure()
    store = CoreRunStore(layout)
    existing = store.reconcile()
    if existing is not None:
        raise typer.BadParameter(
            f"Core is already running with PID {existing.pid} using {existing.data_dir}."
        )
    try:
        claim = store.claim()
    except (OSError, RuntimeError) as error:
        existing = store.reconcile()
        if existing is not None:
            raise typer.BadParameter(
                f"Core is already running with PID {existing.pid} using {existing.data_dir}."
            ) from error
        raise typer.BadParameter(str(error)) from error

    command = [
        sys.executable,
        "-m",
        "tts_studio",
        "serve",
        "--host",
        settings.host,
        "--port",
        str(settings.port),
        "--data-dir",
        str(settings.data_dir),
        "--background-child",
        "--claim-fd",
        str(claim.descriptor),
        "--claim-token",
        claim.token,
    ]
    if include_test_adapters:
        command.append("--include-test-adapters")
    descriptor = -1
    try:
        descriptor = os.open(
            store.log_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "ab") as log:
            descriptor = -1
            process = subprocess.Popen(
                command,
                cwd=str(Path.cwd()),
                env=os.environ.copy(),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
                pass_fds=(claim.descriptor,),
            )
    except BaseException:
        claim.release()
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        _wait_for_core(settings, process=process)
    except RuntimeError as error:
        if store.is_process_alive(process.pid):
            process.terminate()
        record = store.read()
        if record is not None and record.owner_token == claim.token:
            store.clear_if_owner(record.pid, claim.token, claim)
        raise typer.BadParameter(str(error)) from error
    finally:
        claim.release()
    typer.echo(f"Started Core in background (pid={process.pid}).")


def _wait_for_core(
    settings: Settings,
    timeout: float = 15.0,
    process: subprocess.Popen[bytes] | None = None,
) -> None:
    client = CoreClient(_core_url(settings.host, settings.port))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(
                f"Core exited before becoming ready; inspect {settings.data_dir / 'logs' / 'core.log'}"
            )
        try:
            client.system_status()
            return
        except CoreUnavailable:
            time.sleep(0.1)
    raise RuntimeError(
        f"Core did not become ready within {timeout:g} seconds; inspect {settings.data_dir / 'logs' / 'core.log'}"
    )


def _stop_record(store: CoreRunStore, record: CoreRunRecord, timeout: float) -> None:
    current = store.read()
    if current != record:
        return
    try:
        try:
            os.kill(record.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + timeout
        while store.is_process_alive(record.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if store.is_process_alive(record.pid):
            try:
                os.kill(record.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except ProcessLookupError:
                pass
    finally:
        store.clear_if_owner(record.pid, record.owner_token or None)


def _print_system_status(system: SystemStatus) -> None:
    typer.echo(f"Version: {system.version}")
    typer.echo(f"Status: {system.status}")
    typer.echo(f"Data directory: {system.data_dir}")
    if not system.workers:
        typer.echo("Workers: none")
        return

    for worker in system.workers:
        message = f" ({worker.message})" if worker.message else ""
        typer.echo(f"Worker {worker.engine_id}: {worker.status}{message}")


def _print_validation(result: ModelValidationResponse) -> None:
    typer.echo(f"Repository: {result.repository_id}")
    typer.echo(f"Compatible: {'yes' if result.compatible else 'no'}")
    typer.echo(f"Selected engine: {result.selected_engine_id or 'none'}")
    for adapter in result.results:
        if not adapter.available:
            status_text = "unavailable"
        else:
            status_text = "compatible" if adapter.compatible else "incompatible"
        typer.echo(f"Adapter {adapter.engine_id} {adapter.engine_version}: {status_text}")
        for evidence in adapter.evidence:
            typer.echo(f"  {evidence.code}: {evidence.message}")


def _print_model(model: ModelInstallationResponse) -> None:
    typer.echo(f"{model.id}: {model.repository_id}")
    typer.echo(f"  Revision: {model.resolved_commit}")
    typer.echo(f"  Variant: {model.runtime_variant}")
    typer.echo(f"  State: {model.observed_load_state}")
    typer.echo(f"  Size: {model.byte_size} bytes")
    typer.echo(f"  Cache: {model.cache_path}")


def _print_download_progress(job: DownloadJobResponse) -> None:
    typer.echo(f"Phase: {job.phase}")
    if job.total_bytes is None:
        typer.echo(f"Progress: {job.bytes_downloaded} bytes downloaded (size unavailable)")
        return
    percentage = 100 if job.total_bytes == 0 else (job.bytes_downloaded * 100) // job.total_bytes
    typer.echo(f"Progress: {job.bytes_downloaded} / {job.total_bytes} bytes ({percentage}%)")


def _print_json(value: object) -> None:
    document: object
    if isinstance(value, list):
        document = [item.model_dump(mode="json") for item in value]
    elif isinstance(value, BaseModel):
        document = value.model_dump(mode="json")
    else:
        raise TypeError("CLI JSON output requires generated OpenAPI models")
    typer.echo(json.dumps(document, sort_keys=True))


def _print_generation(job: GenerationJobResponse) -> None:
    typer.echo(f"{job.id}: {job.state}")
    typer.echo(f"  Model: {job.model_id}")
    typer.echo(f"  Voice: {job.voice_id}")
    typer.echo(f"  Text: {job.text}")
    if job.error:
        typer.echo(f"  Error: {_safe_job_error_field(job.error, 'code') or 'generation_failed'}")


def _print_artifact(artifact: AudioArtifactResponse) -> None:
    typer.echo(f"{artifact.id}: {artifact.byte_size} bytes")
    typer.echo(f"  Job: {artifact.job_id}")
    typer.echo(f"  Audio: {artifact.audio_url}")


def _exit_terminal_generation(job: GenerationJobResponse, *, json_output: bool) -> NoReturn:
    code = _safe_job_error_field(job.error, "code") or f"generation_{job.state}"
    message = _safe_job_error_field(job.error, "message") or f"Generation {job.state}."
    _exit_generation_cli_error(code, message, job.correlation_id, json_output=json_output)


def _exit_generation_cli_error(
    code: str,
    message: str,
    correlation_id: str,
    *,
    json_output: bool,
) -> NoReturn:
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "error": {
                        "code": code,
                        "message": message,
                        "source": "generation",
                        "retryable": False,
                        "correlation_id": correlation_id,
                        "details": {},
                    }
                },
                sort_keys=True,
            ),
            err=True,
        )
    else:
        typer.echo(f"{code}: {message} (correlation: {correlation_id})", err=True)
    raise typer.Exit(code=4)


def _exit_core_unavailable(url: str, *, json_output: bool) -> NoReturn:
    if json_output:
        error = ErrorEnvelope(
            error=ErrorBody(
                code="core_unavailable",
                correlation_id="unavailable",
                details={},
                message="The Core is unavailable.",
                retryable=True,
                source="transport",
            )
        )
        typer.echo(json.dumps(error.model_dump(mode="json"), sort_keys=True), err=True)
    else:
        typer.echo(f"Unable to reach Core at {url}; start it with 'tts serve'.", err=True)
    raise typer.Exit(code=3)


def _exit_api_error(error: CoreApiError, *, json_output: bool) -> NoReturn:
    if json_output:
        typer.echo(
            json.dumps({"error": error.error.model_dump(mode="json")}, sort_keys=True),
            err=True,
        )
    else:
        typer.echo(
            f"{error.error.code}: {error.error.message} "
            f"(correlation: {error.error.correlation_id})",
            err=True,
        )
    raise typer.Exit(code=4)


def _exit_terminal_download(job: DownloadJobResponse, *, json_output: bool) -> NoReturn:
    if not json_output:
        code = _safe_job_error_field(job.error, "code") or f"download_{job.state}"
        message = _safe_job_error_field(job.error, "message") or f"Download {job.state}."
        typer.echo(f"{code}: {message} (correlation: {job.correlation_id})", err=True)
    raise typer.Exit(code=4)


def _safe_job_error_field(error: dict[str, object] | None, field: str) -> str | None:
    value = error.get(field) if error else None
    return value if isinstance(value, str) else None
