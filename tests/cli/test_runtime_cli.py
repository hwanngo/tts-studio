from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from tts_studio import cli

runner = CliRunner()


def test_runtime_status_json_uses_resolved_data_directory(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class FakeManager:
        def status(self) -> object:
            return SimpleNamespace(
                active_generations={"openai_compatible": "new", "vieneu": "new"},
                previous_generations={"openai_compatible": "old", "vieneu": "old"},
                state="active",
            )

    monkeypatch.setattr(
        cli,
        "_runtime_manager",
        lambda settings: captured.update(settings=settings) or FakeManager(),
    )

    result = runner.invoke(
        cli.app,
        ["runtime", "status", "--json", "--data-dir", str(tmp_path / "data")],
    )

    assert result.exit_code == 0
    assert captured["settings"].data_dir == (tmp_path / "data").resolve()
    assert '"state": "active"' in result.output
    assert '"new"' in result.output


def test_runtime_upgrade_reports_activation(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class FakeManager:
        def upgrade(self, artifact_dir: Path, generation: str) -> object:
            captured.update(artifact_dir=artifact_dir, generation=generation)
            return SimpleNamespace(generation_id=generation)

    monkeypatch.setattr(cli, "_runtime_manager", lambda settings: FakeManager())

    release = tmp_path / "release"
    result = runner.invoke(
        cli.app,
        [
            "runtime",
            "upgrade",
            "--artifact-dir",
            str(release),
            "--generation",
            "new",
            "--data-dir",
            str(tmp_path / "data"),
        ],
    )

    assert result.exit_code == 0
    assert captured == {"artifact_dir": release, "generation": "new"}
    assert "Activated Worker runtime generation new." in result.output


def test_runtime_rollback_reports_previous_generation(monkeypatch, tmp_path: Path) -> None:
    class FakeManager:
        def rollback(self) -> object:
            return SimpleNamespace(
                generation_id="old",
                previous_generations={"openai_compatible": "new", "vieneu": "new"},
            )

    monkeypatch.setattr(cli, "_runtime_manager", lambda settings: FakeManager())
    result = runner.invoke(
        cli.app,
        ["runtime", "rollback", "--data-dir", str(tmp_path / "data")],
    )

    assert result.exit_code == 0
    assert "Rolled back Worker runtime from new to old." in result.output
