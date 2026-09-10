"""Pure renderers and user-owned installation helpers for login services."""

from .installation import ServicePaths, ServicePlatform, uninstall_definition, write_definition
from .lifecycle import (
    LifecycleAdapter,
    LifecycleError,
    LifecycleOperationError,
    LifecycleResult,
    LifecycleUnsupportedError,
    ServiceSnapshot,
)
from .manager import ServiceManager
from .renderers import (
    ServiceDefinition,
    WindowsTaskDefinition,
    render_launchd,
    render_systemd_user_unit,
    render_windows_task,
)

__all__ = [
    "LifecycleAdapter",
    "LifecycleError",
    "LifecycleOperationError",
    "LifecycleResult",
    "LifecycleUnsupportedError",
    "ServiceDefinition",
    "ServiceManager",
    "ServicePaths",
    "ServicePlatform",
    "ServiceSnapshot",
    "WindowsTaskDefinition",
    "render_launchd",
    "render_systemd_user_unit",
    "render_windows_task",
    "uninstall_definition",
    "write_definition",
]
