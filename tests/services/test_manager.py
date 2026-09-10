from pathlib import Path

from tts_studio.services import ServiceDefinition, ServiceManager, ServicePlatform


def _definition(tmp_path: Path) -> ServiceDefinition:
    return ServiceDefinition(
        executable=Path("/opt/tts studio/bin/tts"),
        data_dir=tmp_path / "managed data",
        host="127.0.0.1",
        port=8787,
        name="tts-studio",
    )


def test_linux_install_writes_unit_and_enables_user_service(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []
    manager = ServiceManager(
        _definition(tmp_path),
        platform=ServicePlatform.LINUX,
        home=tmp_path / "home",
        run=lambda command: commands.append(command),
    )

    paths = manager.install()

    assert paths.definition is not None and paths.definition.is_file()
    assert "ExecStart=/opt/tts\\ studio/bin/tts serve --background" in paths.definition.read_text()
    assert commands == [
        ("systemctl", "--user", "daemon-reload"),
        ("systemctl", "--user", "enable", "tts-studio.service"),
        ("systemctl", "--user", "start", "tts-studio.service"),
    ]


def test_macos_uninstall_boots_out_and_removes_definition(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []
    definition = _definition(tmp_path)
    manager = ServiceManager(
        definition,
        platform=ServicePlatform.DARWIN,
        home=tmp_path / "home",
        uid=501,
        run=lambda command: commands.append(command),
    )
    manager.install()

    removed = manager.uninstall()

    assert removed is True
    assert manager.paths.definition is not None and not manager.paths.definition.exists()
    assert commands[-1] == ("launchctl", "bootout", "gui/501/tts-studio")


def test_windows_install_and_uninstall_use_task_scheduler_commands(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []
    manager = ServiceManager(
        _definition(tmp_path),
        platform=ServicePlatform.WINDOWS,
        home=tmp_path / "home",
        run=lambda command: commands.append(command),
    )

    assert manager.install().definition is None
    assert manager.uninstall() is True
    assert commands[0][:6] == ("schtasks", "/Create", "/TN", "tts-studio", "/SC", "ONLOGON")
    assert commands[1] == ("schtasks", "/Delete", "/TN", "tts-studio", "/F")


def test_uninstall_is_idempotent_when_no_definition_exists(tmp_path: Path) -> None:
    commands: list[tuple[str, ...]] = []
    manager = ServiceManager(
        _definition(tmp_path),
        platform=ServicePlatform.LINUX,
        home=tmp_path / "home",
        run=lambda command: commands.append(command),
    )

    assert manager.uninstall() is False
    assert commands == []
