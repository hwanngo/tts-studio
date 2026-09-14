from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from tts_studio import cli
from tts_studio.services import ServicePlatform

runner = CliRunner()


def test_service_install_delegates_to_resolved_manager(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class FakeManager:
        paths = SimpleNamespace(definition=tmp_path / "tts-studio.service")
        platform = ServicePlatform.LINUX

        def install(self) -> object:
            captured["installed"] = True
            return self.paths

    monkeypatch.setattr(
        cli,
        "_service_manager",
        lambda settings, platform: (
            captured.update(settings=settings, platform=platform) or FakeManager()
        ),
        raising=False,
    )

    result = runner.invoke(
        cli.app,
        ["service", "install", "--platform", "linux", "--data-dir", str(tmp_path / "data")],
    )

    assert result.exit_code == 0
    assert captured["platform"] is ServicePlatform.LINUX
    assert captured["installed"] is True
    assert "Installed" in result.output


def test_service_uninstall_is_idempotent_and_reports_no_definition(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    class FakeManager:
        paths = SimpleNamespace(definition=None)
        platform = ServicePlatform.WINDOWS

        def uninstall(self) -> bool:
            captured["uninstalled"] = True
            return False

    monkeypatch.setattr(
        cli,
        "_service_manager",
        lambda settings, platform: captured.update(platform=platform) or FakeManager(),
        raising=False,
    )

    result = runner.invoke(
        cli.app,
        ["service", "uninstall", "--platform", "windows", "--data-dir", str(tmp_path / "data")],
    )

    assert result.exit_code == 0
    assert captured["platform"] is ServicePlatform.WINDOWS
    assert captured["uninstalled"] is True
    assert "not installed" in result.output


def test_service_platform_rejects_unknown_value(tmp_path: Path) -> None:
    result = runner.invoke(
        cli.app,
        ["service", "install", "--platform", "plan9", "--data-dir", str(tmp_path / "data")],
    )

    assert result.exit_code == 2
    assert "platform" in result.output.lower()
