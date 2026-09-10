"""Stable foundation facts advertised by the VieNeu Worker."""

ENGINE_ID = "vieneu"
ENGINE_VERSION = "3.6.3"
PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 0
MAX_CONCURRENCY = 1

VIENEU_REPOSITORY = "pnnbao-ump/VieNeu-TTS-v3-Turbo"
MOSS_CODEC_REPOSITORY = "OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX"
MOSS_CODEC_COMMIT = "ceff0d0749bfb3fa2d61149794ec6feef0d1e1ae"
VARIANT_DIRECTORIES = {"int8": "onnx_int8", "fp32": "onnx_update"}

CAPABILITIES = (
    "health",
    "model_validation",
    "model_download",
    "model_lifecycle",
    "preset_voices",
    "streaming_synthesis",
    "synthesis_cancellation",
    "reference_cloning",
)
