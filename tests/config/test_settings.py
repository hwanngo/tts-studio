from pathlib import Path

from tts_studio.config import Settings


def test_explicit_data_dir_wins(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TTS_STUDIO_DATA_DIR", str(tmp_path / "env"))
    settings = Settings.resolve(data_dir=tmp_path / "explicit")
    assert settings.data_dir == (tmp_path / "explicit").resolve()


def test_environment_data_dir_is_used_without_explicit_value(tmp_path: Path, monkeypatch) -> None:
    environment_data_dir = tmp_path / "environment"
    monkeypatch.setenv("TTS_STUDIO_DATA_DIR", str(environment_data_dir))

    assert Settings.resolve().data_dir == environment_data_dir.resolve()


def test_default_data_dir_uses_current_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TTS_STUDIO_DATA_DIR", raising=False)
    assert Settings.resolve().data_dir == tmp_path / ".tts-studio"
