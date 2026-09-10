from tts_studio_protocol.engine.v1 import engine_pb2


def test_generation_rpc_names_are_frozen() -> None:
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


def test_generation_rpc_streaming_direction_is_frozen() -> None:
    service = engine_pb2.DESCRIPTOR.services_by_name["EngineWorker"]

    for method_name in ("LoadModel", "UnloadModel", "ListVoices", "Align"):
        method = service.methods_by_name[method_name]
        assert method.client_streaming is False
        assert method.server_streaming is False

    synthesize = service.methods_by_name["Synthesize"]
    assert synthesize.client_streaming is False
    assert synthesize.server_streaming is True


def test_generation_rpc_types_are_frozen() -> None:
    service = engine_pb2.DESCRIPTOR.services_by_name["EngineWorker"]
    expected_types = {
        "LoadModel": (
            "tts_studio.engine.v1.LoadModelRequest",
            "tts_studio.engine.v1.LoadModelResponse",
        ),
        "UnloadModel": (
            "tts_studio.engine.v1.UnloadModelRequest",
            "tts_studio.engine.v1.UnloadModelResponse",
        ),
        "ListVoices": (
            "tts_studio.engine.v1.ListVoicesRequest",
            "tts_studio.engine.v1.ListVoicesResponse",
        ),
        "Align": (
            "tts_studio.engine.v1.AlignRequest",
            "tts_studio.engine.v1.AlignResponse",
        ),
        "Synthesize": (
            "tts_studio.engine.v1.SynthesizeRequest",
            "tts_studio.engine.v1.SynthesisEvent",
        ),
        "ValidateReference": (
            "tts_studio.engine.v1.ValidateReferenceRequest",
            "tts_studio.engine.v1.ValidateReferenceResponse",
        ),
    }

    for method_name, (input_type, output_type) in expected_types.items():
        method = service.methods_by_name[method_name]
        assert method.input_type.full_name == input_type
        assert method.output_type.full_name == output_type


def test_generation_message_field_numbers_are_frozen() -> None:
    expected_fields = {
        "LoadModelRequest": {"model_id": 1, "cache_path": 2, "variant": 3},
        "LoadModelResponse": {"loaded": 1, "error": 2},
        "UnloadModelRequest": {"model_id": 1},
        "UnloadModelResponse": {"unloaded": 1, "error": 2},
        "ListVoicesRequest": {"model_id": 1},
        "PresetVoice": {"id": 1, "label": 2, "capabilities": 3},
        "ListVoicesResponse": {"voices": 1, "error": 2},
        "AlignmentCapability": {"units": 1, "languages": 2, "aligner": 3},
        "AlignmentUnit": {
            "text": 1,
            "source_start": 2,
            "source_end": 3,
            "start_frames": 4,
            "end_frames": 5,
            "confidence": 6,
            "estimated": 7,
        },
        "AlignmentResult": {
            "schema_version": 1,
            "transcript": 2,
            "sample_rate_hz": 3,
            "total_frames": 4,
            "unit": 5,
            "aligner": 6,
            "units": 7,
        },
        "AlignRequest": {"model_id": 1, "audio_path": 2, "transcript": 3},
        "AlignResponse": {"result": 1, "error": 2},
        "SynthesizeRequest": {
            "model_id": 1,
            "voice_id": 2,
            "text": 3,
            "reference": 4,
            "provider": 5,
            "options": 6,
        },
        "SynthesisOptions": {"speed": 1, "pitch": 2, "volume": 3},
        "AudioHeader": {"sample_rate_hz": 1, "channels": 2, "sample_format": 3},
        "PcmChunk": {"sequence": 1, "pcm": 2},
        "SynthesisProgress": {"message": 1, "duration_frames": 2},
        "SynthesisResult": {"total_frames": 1, "duration_ms": 2},
        "SynthesisEvent": {"header": 1, "chunk": 2, "progress": 3, "result": 4, "error": 5},
    }

    for message_name, fields in expected_fields.items():
        descriptor = engine_pb2.DESCRIPTOR.message_types_by_name[message_name]
        assert {field.name: field.number for field in descriptor.fields} == fields


def test_synthesis_event_oneof_membership_is_frozen() -> None:
    descriptor = engine_pb2.DESCRIPTOR.message_types_by_name["SynthesisEvent"]

    assert [oneof.name for oneof in descriptor.oneofs] == ["payload"]
    assert [field.name for field in descriptor.oneofs_by_name["payload"].fields] == [
        "header",
        "chunk",
        "progress",
        "result",
        "error",
    ]


def test_synthesis_options_are_optional_and_additive() -> None:
    options = engine_pb2.SynthesisOptions(speed=1.25, pitch=-0.5)
    request = engine_pb2.SynthesizeRequest(model_id="model", text="hello", options=options)

    assert request.HasField("options")
    assert request.options.HasField("speed")
    assert request.options.speed == 1.25
    assert request.options.HasField("pitch")
    assert request.options.pitch == -0.5
    assert not request.options.HasField("volume")


def test_synthesis_request_without_options_preserves_presence() -> None:
    request = engine_pb2.SynthesizeRequest(model_id="model", text="hello")

    assert not request.HasField("options")


def test_synthesis_request_voice_source_oneof_is_frozen() -> None:
    descriptor = engine_pb2.DESCRIPTOR.message_types_by_name["SynthesizeRequest"]

    assert [oneof.name for oneof in descriptor.oneofs] == ["voice_source"]
    assert [field.name for field in descriptor.oneofs_by_name["voice_source"].fields] == [
        "voice_id",
        "reference",
    ]
    assert descriptor.fields_by_name["voice_id"].number == 2
    assert descriptor.fields_by_name["reference"].number == 4


def test_alignment_message_field_types_are_bounded() -> None:
    unit = engine_pb2.DESCRIPTOR.message_types_by_name["AlignmentUnit"]
    assert unit.fields_by_name["source_start"].type == unit.fields_by_name["source_end"].type == 13
    assert unit.fields_by_name["start_frames"].type == unit.fields_by_name["end_frames"].type == 4
    assert unit.fields_by_name["confidence"].type == 2
    assert unit.fields_by_name["estimated"].type == 8

    result = engine_pb2.DESCRIPTOR.message_types_by_name["AlignmentResult"]
    assert result.fields_by_name["sample_rate_hz"].type == 13
    assert result.fields_by_name["total_frames"].type == 4
    assert result.fields_by_name["unit"].type == 9


def test_alignment_contract_limits_and_units_are_documented_values() -> None:
    allowed_units = frozenset(("word", "phoneme", "character"))
    assert all(unit in allowed_units for unit in ("word", "phoneme", "character"))
    transcript_max_chars = 2000
    max_alignment_units = 10000
    max_serialized_result_bytes = 1_048_576
    assert transcript_max_chars == 2000
    assert max_alignment_units == 10000
    assert max_serialized_result_bytes == 1_048_576


def test_alignment_capability_is_runtime_described() -> None:
    capability = engine_pb2.DESCRIPTOR.message_types_by_name["AlignmentCapability"]
    assert capability.fields_by_name["units"].is_repeated
    assert capability.fields_by_name["languages"].is_repeated
    assert capability.fields_by_name["aligner"].type == 9


def test_sample_format_enum_values_are_frozen() -> None:
    sample_format = engine_pb2.DESCRIPTOR.enum_types_by_name["SampleFormat"]

    assert {value.name: value.number for value in sample_format.values} == {
        "UNSPECIFIED": 0,
        "S16LE": 1,
    }


def test_generation_protocol_has_no_style_field() -> None:
    assert all(
        field.name != "style"
        for message in engine_pb2.DESCRIPTOR.message_types_by_name.values()
        for field in message.fields
    )
