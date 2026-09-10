import tomllib
from pathlib import Path

from tts_studio_protocol.engine.v1 import engine_pb2


def test_v1_contract_exposes_versioned_worker_methods() -> None:
    service = engine_pb2.DESCRIPTOR.services_by_name["EngineWorker"]
    assert [method.name for method in service.methods] == [
        "Describe",
        "Health",
        "ValidateModel",
        "ValidateReference",
        "DownloadModel",
        "LoadModel",
        "UnloadModel",
        "ListVoices",
        "Align",
        "Synthesize",
    ]
    assert engine_pb2.DESCRIPTOR.package == "tts_studio.engine.v1"


def test_describe_alignment_capability_field_is_additive() -> None:
    describe = engine_pb2.DESCRIPTOR.message_types_by_name["DescribeResponse"]
    alignment = describe.fields_by_name["alignment"]

    assert alignment.number == 6
    assert alignment.message_type.full_name == "tts_studio.engine.v1.AlignmentCapability"


def test_model_management_rpc_streaming_contract_is_frozen() -> None:
    service = engine_pb2.DESCRIPTOR.services_by_name["EngineWorker"]
    validate = service.methods_by_name["ValidateModel"]
    download = service.methods_by_name["DownloadModel"]

    assert validate.input_type.full_name == "tts_studio.engine.v1.ValidateModelRequest"
    assert validate.output_type.full_name == "tts_studio.engine.v1.ValidateModelResponse"
    assert validate.client_streaming is False
    assert validate.server_streaming is False

    assert download.input_type.full_name == "tts_studio.engine.v1.DownloadModelRequest"
    assert download.output_type.full_name == "tts_studio.engine.v1.DownloadModelEvent"
    assert download.client_streaming is False
    assert download.server_streaming is True


def test_model_management_message_field_numbers_are_frozen() -> None:
    expected_fields = {
        "ModelVariant": {"id": 1, "label": 2},
        "CompatibilityEvidence": {"code": 1, "message": 2},
        "WorkerError": {"code": 1, "message": 2, "retryable": 3, "details": 4},
        "ValidateModelRequest": {"repository_id": 1, "requested_revision": 2},
        "ValidateModelResponse": {
            "repository_id": 1,
            "requested_revision": 2,
            "resolved_commit": 3,
            "compatible": 4,
            "engine_id": 5,
            "engine_version": 6,
            "required_files": 7,
            "available_variants": 8,
            "estimated_bytes": 9,
            "evidence": 10,
            "error": 11,
        },
        "DownloadModelRequest": {
            "repository_id": 1,
            "resolved_commit": 2,
            "variant": 3,
            "staging_destination": 4,
        },
        "ManifestFile": {"relative_path": 1, "byte_size": 2, "sha256": 3},
        "ModelManifest": {
            "repository_id": 1,
            "resolved_commit": 2,
            "variant": 3,
            "files": 4,
            "byte_size": 5,
        },
        "DownloadProgress": {
            "sequence": 1,
            "phase": 2,
            "bytes_downloaded": 3,
            "total_bytes": 4,
            "message": 5,
            "warnings": 6,
        },
        "DownloadModelEvent": {"progress": 1, "manifest": 2, "error": 3},
    }

    for message_name, fields in expected_fields.items():
        descriptor = engine_pb2.DESCRIPTOR.message_types_by_name[message_name]
        assert {field.name: field.number for field in descriptor.fields} == fields


def test_reference_validation_message_field_numbers_are_frozen() -> None:
    expected_fields = {
        "ReferenceAudio": {"reference_path": 1, "transcript": 2},
        "ValidateReferenceRequest": {
            "model_id": 1,
            "reference_path": 2,
            "transcript": 3,
        },
        "ReferenceMetadata": {
            "sample_rate_hz": 1,
            "channels": 2,
            "duration_ms": 3,
            "byte_size": 4,
            "container": 5,
        },
        "ValidateReferenceResponse": {
            "valid": 1,
            "metadata": 2,
            "evidence": 3,
            "error": 4,
        },
    }

    for message_name, fields in expected_fields.items():
        descriptor = engine_pb2.DESCRIPTOR.message_types_by_name[message_name]
        assert {field.name: field.number for field in descriptor.fields} == fields


def test_download_phase_enum_values_are_frozen() -> None:
    phase = engine_pb2.DESCRIPTOR.enum_types_by_name["DownloadPhase"]
    assert {value.name: value.number for value in phase.values} == {
        "DOWNLOAD_PHASE_UNSPECIFIED": 0,
        "DOWNLOAD_PHASE_DOWNLOADING": 1,
        "DOWNLOAD_PHASE_VERIFYING": 2,
        "DOWNLOAD_PHASE_FINALIZING": 3,
    }


def test_distribution_requires_generator_compatible_runtime_and_typing_dependencies() -> None:
    pyproject_path = Path(__file__).parents[1] / "pyproject.toml"
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    assert pyproject["project"]["dependencies"] == [
        "grpcio>=1.83.1",
        "protobuf>=7.35.1",
        "types-grpcio>=1.83.0.20260730",
        "types-protobuf>=7.35.1.20260827",
    ]
