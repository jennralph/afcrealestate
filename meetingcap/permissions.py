"""Permission reporting (spec 2, 20).

The application never elevates and never bypasses an OS privacy control.
When a capability is unavailable, the user is told exactly which permission
is missing and where to grant it — never a Python stack trace.

``check_permissions()`` distinguishes:

    SUPPORTED_AND_ALLOWED
    SUPPORTED_BUT_PERMISSION_MISSING
    UNSUPPORTED
    DEVICE_UNAVAILABLE
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import List, Optional


class PermissionStatus(str, enum.Enum):
    SUPPORTED_AND_ALLOWED = "SUPPORTED_AND_ALLOWED"
    SUPPORTED_BUT_PERMISSION_MISSING = "SUPPORTED_BUT_PERMISSION_MISSING"
    UNSUPPORTED = "UNSUPPORTED"
    DEVICE_UNAVAILABLE = "DEVICE_UNAVAILABLE"
    DEPENDENCY_MISSING = "DEPENDENCY_MISSING"

    @property
    def ok(self) -> bool:
        return self is PermissionStatus.SUPPORTED_AND_ALLOWED


@dataclass
class PermissionReport:
    """One capability and what, if anything, the user must do about it."""

    component: str                       # "System audio" / "Microphone"
    status: PermissionStatus
    detail: str = ""
    remedy: str = ""

    @property
    def ok(self) -> bool:
        return self.status.ok

    def as_dict(self) -> dict:
        return {
            "component": self.component,
            "status": self.status.value,
            "detail": self.detail,
            "remedy": self.remedy,
        }

    def render(self) -> str:
        lines = [f"{self.component}: {self.status.value}"]
        if self.detail:
            lines.append(f"  {self.detail}")
        if self.remedy:
            for line in self.remedy.strip().splitlines():
                lines.append(f"  {line}")
        lines.append("  No administrator password is required by this application.")
        return "\n".join(lines)


NO_ELEVATION_NOTE = "No administrator password is required by this application."


def check_permissions(backend=None) -> List[PermissionReport]:
    """Report the state of every capability this platform can offer.

    Delegates to the platform backend so that OS-specific detection stays out
    of the shared code path (spec 4).
    """
    if backend is None:
        from .audio.registry import get_backend
        backend = get_backend()
    return backend.check_permissions()


def summarize(reports: List[PermissionReport]) -> Optional[str]:
    """Return an explanatory message when something is blocking, else ``None``."""
    problems = [r for r in reports if not r.ok]
    if not problems:
        return None
    return "\n\n".join(r.render() for r in problems)


def blocking(reports: List[PermissionReport]) -> List[PermissionReport]:
    """Reports that prevent capture of the component they describe."""
    return [r for r in reports if not r.ok]
