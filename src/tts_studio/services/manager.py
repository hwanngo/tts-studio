"""User-level service installation backed by the pure platform renderers."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .installation import ServicePaths, ServicePlatform, uninstall_definition, write_definition
from .renderers import (
    ServiceDefinition,
    render_launchd,
    render_systemd_user_unit,
    render_windows_task,
)

CommandRunner = Callable[[tuple[str, ...]], None]


@dataclass(slots=True)
class ServiceManager:
    """Install, activate, and remove one per-user Core service."""

    definition: ServiceDefinition
    platform: ServicePlatform
    home: Path
    uid: int | None = None
    run: CommandRunner | None = None
    paths: ServicePaths = field(init=False)

    def __post_init__(self) -> None:
        self.paths = ServicePaths.resolve(self.platform, self.definition.name, self.home)
        if self.run is None:
            self.run = _run_command

    def install(self) -> ServicePaths:
        if self.platform is ServicePlatform.DARWIN:
            write_definition(self.paths, render_launchd(self.definition))
            self._run(("launchctl", "bootstrap", self._gui_domain(), str(self.paths.definition)))
        elif self.platform is ServicePlatform.LINUX:
            write_definition(self.paths, render_systemd_user_unit(self.definition))
            service_name = f"{self.definition.name}.service"
            self._run(("systemctl", "--user", "daemon-reload"))
            self._run(("systemctl", "--user", "enable", service_name))
            self._run(("systemctl", "--user", "start", service_name))
        else:
            task = render_windows_task(self.definition)
            self._run(task.create_command)
        return self.paths

    def uninstall(self) -> bool:
        if self.platform is ServicePlatform.WINDOWS:
            self._run(render_windows_task(self.definition).delete_command)
            return True
        if self.paths.definition is None or not self.paths.definition.exists():
            return False
        if self.platform is ServicePlatform.DARWIN:
            self._run(("launchctl", "bootout", self._gui_domain() + "/" + self.definition.name))
        else:
            self._run(
                (
                    "systemctl",
                    "--user",
                    "disable",
                    "--now",
                    f"{self.definition.name}.service",
                )
            )
            self._run(("systemctl", "--user", "daemon-reload"))
        return uninstall_definition(self.paths)

    def _gui_domain(self) -> str:
        return f"gui/{self.uid if self.uid is not None else os.getuid()}"

    def _run(self, command: tuple[str, ...]) -> None:
        assert self.run is not None
        self.run(command)


def _run_command(command: tuple[str, ...]) -> None:
    subprocess.run(command, check=True)
