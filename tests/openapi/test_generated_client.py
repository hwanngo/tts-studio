import importlib.util
from pathlib import Path
from typing import get_args, get_type_hints

from tts_studio import client
from tts_studio.generated import api

_GENERATOR_PATH = Path(__file__).parents[2] / "scripts" / "generate_openapi_client.py"
_SPEC = importlib.util.spec_from_file_location("generate_openapi_client", _GENERATOR_PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
generate = _MODULE.generate
render_schema = _MODULE.render_schema

_CHECK_PATH = Path(__file__).parents[2] / "scripts" / "check_openapi.py"
_CHECK_SPEC = importlib.util.spec_from_file_location("check_openapi", _CHECK_PATH)
assert _CHECK_SPEC and _CHECK_SPEC.loader
_CHECK_MODULE = importlib.util.module_from_spec(_CHECK_SPEC)
_CHECK_SPEC.loader.exec_module(_CHECK_MODULE)
check_openapi = _CHECK_MODULE.main


def test_openapi_generation_is_deterministic(tmp_path: Path) -> None:
    first = generate(tmp_path / "one.ts", tmp_path / "one.py")
    second = generate(tmp_path / "two.ts", tmp_path / "two.py")

    assert first == second
    assert (tmp_path / "one.py").read_text(encoding="utf-8") == (tmp_path / "two.py").read_text(
        encoding="utf-8"
    )


def test_checked_in_clients_match_generator(tmp_path: Path) -> None:
    generated_typescript = tmp_path / "api.ts"
    generated_python = tmp_path / "api.py"
    generated = generate(generated_typescript, generated_python)
    repository_root = Path(__file__).parents[2]
    checked_in_typescript = repository_root / "web" / "src" / "generated" / "api.ts"
    checked_in_python = repository_root / "src" / "tts_studio" / "generated" / "api.py"

    assert checked_in_typescript.read_text(encoding="utf-8") == generated
    assert checked_in_python.read_text(encoding="utf-8") == generated_python.read_text(
        encoding="utf-8"
    )


def test_nullable_json_schema_renders_as_a_typescript_null_union() -> None:
    rendered = render_schema(
        {
            "components": {
                "schemas": {
                    "Example": {
                        "type": "object",
                        "properties": {"value": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
                        "required": ["value"],
                    }
                }
            }
        }
    )

    assert "value: string | null;" in rendered
    assert "string | unknown" not in rendered


def test_core_client_uses_the_generated_openapi_models() -> None:
    assert get_type_hints(client.CoreApiError.__init__)["error"] is api.ErrorBody
    assert get_type_hints(client.CoreClient.system_status)["return"] is api.SystemStatus
    assert get_type_hints(client.CoreClient.settings)["return"] is api.SettingsResponse
    assert get_type_hints(client.CoreClient.clear_retention)["return"] is api.ClearRetentionResponse
    assert get_type_hints(client.CoreClient.runtime_status)["return"] is api.RuntimeResponse
    assert get_type_hints(client.CoreClient.service_status)["return"] is api.ServiceResponse
    assert get_type_hints(client.CoreClient.install_service)["return"] is api.ServiceOperationResponse
    update_types = get_type_hints(client.CoreClient.update_settings)
    assert object not in get_args(update_types["retain_audio_by_default"])
    assert client._UnsetType in get_args(update_types["retain_audio_by_default"])
    assert get_type_hints(client.CoreClient.validate_model)["return"] is api.ModelValidationResponse
    assert (
        get_type_hints(client.CoreClient.list_models)["return"]
        == list[api.ModelInstallationResponse]
    )
    assert get_type_hints(client.CoreClient.start_download)["return"] is api.DownloadJobResponse
    assert get_type_hints(client.CoreClient.list_voices)["return"] == list[api.VoiceResponse]
    assert get_type_hints(client.CoreClient.preview_voice)["return"] is bytes
    create_generation_types = get_type_hints(client.CoreClient.create_generation)
    assert create_generation_types["return"] is api.GenerationJobResponse
    assert create_generation_types["speed"] == float | None
    assert create_generation_types["pitch"] == float | None
    assert create_generation_types["volume"] == float | None
    assert get_type_hints(client.CoreClient.get_generation)["return"] is api.GenerationJobResponse
    assert (
        get_type_hints(client.CoreClient.list_generations)["return"]
        == list[api.GenerationJobResponse]
    )
    assert get_type_hints(client.CoreClient.cancel_generation)["return"] is api.GenerationJobResponse
    assert get_type_hints(client.CoreClient.list_history)["return"] == list[api.AudioArtifactResponse]


def test_openapi_drift_check_covers_the_python_client(tmp_path: Path) -> None:
    repository_root = Path(__file__).parents[2]
    typescript = tmp_path / "web" / "src" / "generated" / "api.ts"
    python = tmp_path / "src" / "tts_studio" / "generated" / "api.py"
    typescript.parent.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    typescript.write_text(
        (repository_root / "web" / "src" / "generated" / "api.ts").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    python.write_text(
        (repository_root / "src" / "tts_studio" / "generated" / "api.py").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )

    assert check_openapi(tmp_path) == 0
    python.write_text("stale\n", encoding="utf-8")
    assert check_openapi(tmp_path) == 1
