from tts_studio_protocol.engine.v1 import engine_pb2


def test_validation_contract_preserves_compatibility_evidence() -> None:
    response = engine_pb2.ValidateModelResponse(
        repository_id="example/model",
        requested_revision="main",
        resolved_commit="a" * 40,
        compatible=True,
        engine_id="fake",
        engine_version="1.0.0",
        required_files=["config.json", "model.onnx"],
        available_variants=[engine_pb2.ModelVariant(id="int8", label="INT8")],
        estimated_bytes=2048,
        evidence=[
            engine_pb2.CompatibilityEvidence(
                code="fixture_supported", message="Fake fixture is supported"
            )
        ],
    )

    restored = engine_pb2.ValidateModelResponse.FromString(response.SerializeToString())

    assert restored.repository_id == "example/model"
    assert restored.requested_revision == "main"
    assert restored.resolved_commit == "a" * 40
    assert restored.required_files == ["config.json", "model.onnx"]
    assert restored.available_variants[0].id == "int8"
    assert restored.estimated_bytes == 2048
    assert restored.evidence[0].code == "fixture_supported"
    assert not restored.HasField("error")


def test_validation_contract_carries_structured_errors() -> None:
    response = engine_pb2.ValidateModelResponse(
        repository_id="example/unsupported",
        compatible=False,
        error=engine_pb2.WorkerError(
            code="model_incompatible",
            message="No compatible fixture was found",
            retryable=False,
            details={"adapter": "fake"},
        ),
    )

    assert response.HasField("error")
    assert response.error.code == "model_incompatible"
    assert response.error.details == {"adapter": "fake"}


def test_download_progress_distinguishes_unknown_and_known_totals() -> None:
    unknown = engine_pb2.DownloadProgress(
        sequence=1,
        phase=engine_pb2.DOWNLOAD_PHASE_DOWNLOADING,
        bytes_downloaded=512,
    )
    known = engine_pb2.DownloadProgress(
        sequence=2,
        phase=engine_pb2.DOWNLOAD_PHASE_DOWNLOADING,
        bytes_downloaded=1024,
        total_bytes=4096,
    )

    assert not unknown.HasField("total_bytes")
    assert known.HasField("total_bytes")
    assert known.total_bytes == 4096


def test_download_event_payloads_are_mutually_exclusive() -> None:
    event = engine_pb2.DownloadModelEvent(
        progress=engine_pb2.DownloadProgress(
            sequence=1,
            phase=engine_pb2.DOWNLOAD_PHASE_VERIFYING,
            bytes_downloaded=1024,
        )
    )

    assert event.WhichOneof("payload") == "progress"

    event.manifest.CopyFrom(
        engine_pb2.ModelManifest(
            repository_id="example/model",
            resolved_commit="b" * 40,
            variant="int8",
            files=[
                engine_pb2.ManifestFile(relative_path="model.onnx", byte_size=1024, sha256="c" * 64)
            ],
            byte_size=1024,
        )
    )

    assert event.WhichOneof("payload") == "manifest"
    assert not event.HasField("progress")
    assert event.manifest.files[0].relative_path == "model.onnx"
