"""Validate the immutable local cache required by the VieNeu hardware gate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MODEL_COMMIT = "8b7e9cffb4b41918cb638b9f62f0a751184d14a6"
CODEC_REVISION = "ceff0d0749bfb3fa2d61149794ec6feef0d1e1ae"
SDK_VERSION = "3.6.3"
GRAPH_FILES = (
    "config.json",
    "tokenizer.json",
    "vieneu_acoustic_cached.onnx",
    "vieneu_decode_step.onnx",
    "vieneu_prefill.onnx",
    "vieneu_backbone_shared.data",
    "vieneu_v3_heads.npz",
)
CODEC_FILES = (
    "codec_browser_onnx_meta.json",
    "moss_audio_tokenizer_decode_full.onnx",
    "moss_audio_tokenizer_decode_shared.data",
    "moss_audio_tokenizer_decode_step.onnx",
    "moss_audio_tokenizer_encode.data",
    "moss_audio_tokenizer_encode.onnx",
)
CLONING_FILES = ("denoiser.onnx", "speaker_encoder.onnx")


def _has_symbolic_link_component(cache_root: Path, path: Path) -> bool:
    """Check every child component without resolving into a linked directory."""
    try:
        relative_path = path.relative_to(cache_root)
    except ValueError:
        return True
    current = cache_root
    for component in relative_path.parts:
        current /= component
        if current.is_symlink():
            return True
    return False


def _is_safe_directory(cache_root: Path, path: Path) -> bool:
    return not _has_symbolic_link_component(cache_root, path) and path.is_dir()


def _is_safe_regular_file(cache_root: Path, path: Path) -> bool:
    return not _has_symbolic_link_component(cache_root, path) and path.is_file()


def cache_prerequisite_reason(model_cache: Path | None) -> str | None:
    """Return a precise failure reason without following cache symlinks."""
    if model_cache is None:
        return "set TTS_STUDIO_VIENEU_MODEL_CACHE to an exact local VieNeu cache"
    if model_cache.is_symlink() or not model_cache.is_dir():
        return f"VieNeu cache directory does not exist or is a symbolic link: {model_cache}"
    marker = model_cache / ".vieneu-model-cache-marker"
    if not _is_safe_regular_file(model_cache, marker):
        return "VieNeu cache marker is missing, unsafe, or is not a regular file"
    try:
        provenance = json.loads(marker.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return "VieNeu cache marker is not valid JSON"
    if not isinstance(provenance, dict):
        return "VieNeu cache marker must contain a JSON object"
    if provenance.get("model_commit") != MODEL_COMMIT:
        return "VieNeu cache marker does not contain the required pinned model revision"
    if provenance.get("codec_revision") != CODEC_REVISION:
        return "VieNeu cache marker does not contain the required pinned codec revision"
    if provenance.get("sdk_version") != SDK_VERSION:
        return "VieNeu cache marker does not contain the required VieNeu SDK version"
    for variant in ("int8", "fp32"):
        variant_root = model_cache / "models" / "vieneu" / "backbone" / variant
        if not _is_safe_directory(model_cache, variant_root):
            return f"VieNeu cache is missing the safe {variant} model directory"
        if not all(_is_safe_regular_file(model_cache, variant_root / name) for name in GRAPH_FILES):
            return f"VieNeu cache is missing safe {variant} model files"
    codec_root = model_cache / "models" / "vieneu" / "codec"
    if not _is_safe_directory(model_cache, codec_root):
        return "VieNeu cache is missing the safe codec directory"
    if not all(_is_safe_regular_file(model_cache, codec_root / name) for name in CODEC_FILES):
        return "VieNeu cache is missing safe codec files"
    cloning_root = model_cache / "models" / "vieneu" / "cloning"
    if not _is_safe_directory(model_cache, cloning_root):
        return "VieNeu cache is missing the safe cloning directory"
    if not all(_is_safe_regular_file(model_cache, cloning_root / name) for name in CLONING_FILES):
        return "VieNeu cache is missing safe cloning files"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", required=True, type=Path)
    arguments = parser.parse_args()
    if reason := cache_prerequisite_reason(arguments.cache):
        print(f"VieNeu hardware cache preflight failed: {reason}", file=sys.stderr)
        return 1
    print("VieNeu hardware cache preflight passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
