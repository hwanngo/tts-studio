from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

_CACHE_VALIDATOR = runpy.run_path(
    Path(__file__).resolve().parents[2] / "scripts" / "check_vieneu_hardware_cache.py"
)
CODEC_FILES = _CACHE_VALIDATOR["CODEC_FILES"]
CODEC_REVISION = _CACHE_VALIDATOR["CODEC_REVISION"]
GRAPH_FILES = _CACHE_VALIDATOR["GRAPH_FILES"]
MODEL_COMMIT = _CACHE_VALIDATOR["MODEL_COMMIT"]
SDK_VERSION = _CACHE_VALIDATOR["SDK_VERSION"]
cache_prerequisite_reason = _CACHE_VALIDATOR["cache_prerequisite_reason"]


def _write_cache(root: Path) -> None:
    root.mkdir()
    (root / ".vieneu-model-cache-marker").write_text(
        json.dumps(
            {
                "model_commit": MODEL_COMMIT,
                "codec_revision": CODEC_REVISION,
                "sdk_version": SDK_VERSION,
            }
        ),
        encoding="utf-8",
    )
    for variant in ("int8", "fp32"):
        variant_root = root / "models" / "vieneu" / "backbone" / variant
        variant_root.mkdir(parents=True)
        for filename in GRAPH_FILES:
            (variant_root / filename).write_text("fixture", encoding="utf-8")
    codec_root = root / "models" / "vieneu" / "codec"
    codec_root.mkdir()
    for filename in CODEC_FILES:
        (codec_root / filename).write_text("fixture", encoding="utf-8")
    cloning_root = root / "models" / "vieneu" / "cloning"
    cloning_root.mkdir()
    for filename in ("denoiser.onnx", "speaker_encoder.onnx"):
        (cloning_root / filename).write_text("fixture", encoding="utf-8")


def test_accepts_complete_pinned_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    _write_cache(cache)

    assert cache_prerequisite_reason(cache) is None


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("not-json", "not valid JSON"),
        ("[]", "must contain a JSON object"),
        (json.dumps({"model_commit": "wrong"}), "required pinned model revision"),
    ],
)
def test_rejects_invalid_marker_before_hardware_test(
    tmp_path: Path, marker: str, expected: str
) -> None:
    cache = tmp_path / "cache"
    _write_cache(cache)
    (cache / ".vieneu-model-cache-marker").write_text(marker, encoding="utf-8")

    assert expected in (cache_prerequisite_reason(cache) or "")


def test_rejects_missing_required_model_or_codec_file(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    _write_cache(cache)
    (cache / "models" / "vieneu" / "backbone" / "int8" / GRAPH_FILES[0]).unlink()

    assert "safe int8 model files" in (cache_prerequisite_reason(cache) or "")


def test_rejects_symbolic_linked_cache_file(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    _write_cache(cache)
    replacement = tmp_path / "replacement.onnx"
    replacement.write_text("fixture", encoding="utf-8")
    required_file = cache / "models" / "vieneu" / "codec" / CODEC_FILES[0]
    required_file.unlink()
    required_file.symlink_to(replacement)

    assert "safe codec files" in (cache_prerequisite_reason(cache) or "")


def test_rejects_symbolic_linked_models_directory(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    _write_cache(cache)
    external_models = tmp_path / "external-models"
    (cache / "models").rename(external_models)
    (cache / "models").symlink_to(external_models, target_is_directory=True)

    assert "safe int8 model directory" in (cache_prerequisite_reason(cache) or "")


def test_rejects_symbolic_linked_nested_model_directory(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    _write_cache(cache)
    external_backbone = tmp_path / "external-backbone"
    backbone = cache / "models" / "vieneu" / "backbone"
    backbone.rename(external_backbone)
    backbone.symlink_to(external_backbone, target_is_directory=True)

    assert "safe int8 model directory" in (cache_prerequisite_reason(cache) or "")


@pytest.mark.parametrize("filename", ["denoiser.onnx", "speaker_encoder.onnx"])
def test_rejects_missing_cloning_runtime_file(tmp_path: Path, filename: str) -> None:
    cache = tmp_path / "cache"
    _write_cache(cache)
    (cache / "models" / "vieneu" / "cloning" / filename).unlink()

    assert "safe cloning files" in (cache_prerequisite_reason(cache) or "")


def test_rejects_symbolic_linked_cloning_directory(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    _write_cache(cache)
    cloning = cache / "models" / "vieneu" / "cloning"
    external_cloning = tmp_path / "external-cloning"
    cloning.rename(external_cloning)
    cloning.symlink_to(external_cloning, target_is_directory=True)

    assert "safe cloning directory" in (cache_prerequisite_reason(cache) or "")
