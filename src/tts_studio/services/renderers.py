"""Platform service definitions without installation or process side effects."""

from __future__ import annotations

import plistlib
from dataclasses import dataclass
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

from .installation import validate_service_name


@dataclass(frozen=True, slots=True)
class ServiceDefinition:
    """Resolved inputs used by every platform renderer."""

    executable: Path
    data_dir: Path
    host: str
    port: int
    name: str = "tts-studio"

    def __post_init__(self) -> None:
        if not self.executable.is_absolute() or not self.data_dir.is_absolute():
            raise ValueError("executable and data_dir must be resolved absolute paths")
        validate_service_name(self.name)
        if not self.host or not 0 < self.port <= 65535:
            raise ValueError("host and port must be valid resolved server settings")

    @property
    def start_arguments(self) -> tuple[str, ...]:
        return (
            "serve",
            "--background",
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--data-dir",
            str(self.data_dir),
        )

    @property
    def stop_arguments(self) -> tuple[str, ...]:
        return ("stop", "--data-dir", str(self.data_dir))


@dataclass(frozen=True, slots=True)
class WindowsTaskDefinition:
    """Windows XML plus side-effect-free commands an installer may execute later."""

    xml: str
    create_command: tuple[str, ...]
    stop_command: tuple[str, ...]
    delete_command: tuple[str, ...]


def render_launchd(definition: ServiceDefinition) -> str:
    """Render a launchd plist; loading or unloading it is intentionally out of scope."""

    document = {
        "Label": definition.name,
        "ProgramArguments": [str(definition.executable), *definition.start_arguments],
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(definition.data_dir.parent),
        "StandardOutPath": str(definition.data_dir / "logs" / "service.log"),
        "StandardErrorPath": str(definition.data_dir / "logs" / "service.log"),
    }
    return plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=False).decode("utf-8")


def render_systemd_user_unit(definition: ServiceDefinition) -> str:
    """Render a systemd user unit using escaped, non-shell command arguments."""

    start = _systemd_command(definition.executable, definition.start_arguments)
    stop = _systemd_command(definition.executable, definition.stop_arguments)
    return (
        "[Unit]\n"
        "Description=TTS Studio Core\n"
        "After=default.target\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={start}\n"
        f"ExecStop={stop}\n"
        "RemainAfterExit=yes\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def render_windows_task(definition: ServiceDefinition) -> WindowsTaskDefinition:
    """Render Task Scheduler XML and inert commands for a per-user task."""

    task = Element(
        "Task",
        {
            "version": "1.4",
            "xmlns": "http://schemas.microsoft.com/windows/2004/02/mit/task",
        },
    )
    registration = SubElement(task, "RegistrationInfo")
    SubElement(registration, "Description").text = "TTS Studio Core"
    triggers = SubElement(task, "Triggers")
    SubElement(triggers, "LogonTrigger")
    principals = SubElement(task, "Principals")
    principal = SubElement(principals, "Principal", {"id": "Author"})
    SubElement(principal, "LogonType").text = "InteractiveToken"
    settings = SubElement(task, "Settings")
    SubElement(settings, "MultipleInstancesPolicy").text = "IgnoreNew"
    actions = SubElement(task, "Actions", {"Context": "Author"})
    exec_action = SubElement(actions, "Exec")
    SubElement(exec_action, "Command").text = str(definition.executable)
    SubElement(exec_action, "Arguments").text = _windows_arguments(definition.start_arguments)
    xml = tostring(task, encoding="unicode")
    create = (
        "schtasks",
        "/Create",
        "/TN",
        definition.name,
        "/SC",
        "ONLOGON",
        "/TR",
        _windows_command_line(definition.executable, definition.start_arguments),
        "/F",
    )
    stop = (str(definition.executable), *definition.stop_arguments)
    delete = ("schtasks", "/Delete", "/TN", definition.name, "/F")
    return WindowsTaskDefinition(
        xml=xml,
        create_command=create,
        stop_command=stop,
        delete_command=delete,
    )


def _systemd_command(executable: Path, arguments: tuple[str, ...]) -> str:
    return " ".join(_systemd_quote(value) for value in (str(executable), *arguments))


def _systemd_quote(value: str) -> str:
    safe = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./:-")
    return "".join(char if char in safe else f"\\{char}" for char in value)


def _windows_arguments(arguments: tuple[str, ...]) -> str:
    return " ".join(_windows_quote(value) for value in arguments)


def _windows_command_line(executable: Path, arguments: tuple[str, ...]) -> str:
    return " ".join((_windows_quote(str(executable)), _windows_arguments(arguments)))


def _windows_quote(value: str) -> str:
    return f'"{value.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
