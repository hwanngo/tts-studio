from pathlib import Path
from xml.etree.ElementTree import fromstring

import pytest

from tts_studio.services import (
    ServiceDefinition,
    render_launchd,
    render_systemd_user_unit,
    render_windows_task,
)


@pytest.fixture
def definition(tmp_path: Path) -> ServiceDefinition:
    return ServiceDefinition(
        executable=Path("/opt/tts studio/bin/tts"),
        data_dir=tmp_path / "managed data",
        host="127.0.0.1",
        port=8787,
        name="com.example.tts-studio",
    )


def test_launchd_renderer_uses_escaped_arguments_and_public_lifecycle(definition: ServiceDefinition) -> None:
    rendered = render_launchd(definition)

    assert "<key>ProgramArguments</key>" in rendered
    assert "<string>/opt/tts studio/bin/tts</string>" in rendered
    assert "<string>serve</string>" in rendered
    assert "<string>--background</string>" in rendered
    assert "<string>--data-dir</string>" in rendered
    assert "<string>127.0.0.1</string>" in rendered
    assert "<string>8787</string>" in rendered
    assert "&amp;" not in rendered


def test_systemd_renderer_contains_start_and_stop_commands(definition: ServiceDefinition) -> None:
    rendered = render_systemd_user_unit(definition)

    assert "ExecStart=/opt/tts\\ studio/bin/tts serve --background" in rendered
    assert f"--data-dir {_systemd_escape(str(definition.data_dir))}" in rendered
    assert "--host 127.0.0.1 --port 8787" in rendered
    assert "ExecStop=/opt/tts\\ studio/bin/tts stop" in rendered
    assert "[Service]" in rendered


def test_windows_renderer_returns_xml_and_schtasks_definition(definition: ServiceDefinition) -> None:
    rendered = render_windows_task(definition)

    assert "<Task" in rendered.xml
    assert "http://schemas.microsoft.com/windows/2004/02/mit/task" in rendered.xml
    assert "<Command>/opt/tts studio/bin/tts</Command>" in rendered.xml
    arguments = fromstring(rendered.xml).find(
        ".//{http://schemas.microsoft.com/windows/2004/02/mit/task}Arguments"
    )
    assert arguments is not None
    assert arguments.text is not None
    assert "serve" in arguments.text and "--background" in arguments.text
    assert str(definition.data_dir) in arguments.text
    assert rendered.create_command[:2] == ("schtasks", "/Create")
    assert rendered.create_command[5:7] == ("ONLOGON", "/TR")
    assert rendered.create_command[7].startswith('"/opt/tts studio/bin/tts"')
    assert rendered.stop_command == (
        "/opt/tts studio/bin/tts",
        "stop",
        "--data-dir",
        str(definition.data_dir),
    )
    assert rendered.delete_command == ("schtasks", "/Delete", "/TN", definition.name, "/F")


def test_renderers_are_pure_and_do_not_create_paths(definition: ServiceDefinition) -> None:
    assert not definition.data_dir.exists()
    render_launchd(definition)
    render_systemd_user_unit(definition)
    render_windows_task(definition)

    assert not definition.data_dir.exists()


def test_service_definition_rejects_unsafe_manager_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="name"):
        ServiceDefinition(
            executable=Path("/opt/tts"),
            data_dir=tmp_path,
            host="127.0.0.1",
            port=8787,
            name="bad name",
        )


def _systemd_escape(value: str) -> str:
    return value.replace(" ", "\\ ")
