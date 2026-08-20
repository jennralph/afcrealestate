"""The platform backend contract (spec 4).

``platform.system()`` picks one of these; nothing above this line contains
OS-specific code.
"""

from __future__ import annotations

from typing import List, Optional

from ..permissions import PermissionReport, PermissionStatus
from .base import AudioSource, DeviceInfo


class PlatformBackend:
    """Device enumeration, source construction and permission state for one OS."""

    name = "unknown"

    #: True when system capture is bound to the current render endpoint and
    #: must therefore be reopened when the default output changes (Windows
    #: WASAPI loopback, PipeWire sink monitors).  False where capture taps
    #: the mix itself and survives an endpoint change (macOS
    #: ScreenCaptureKit) — there a change is logged but nothing is reopened.
    system_capture_follows_default_endpoint = True

    # -- enumeration -----------------------------------------------------

    def list_devices(self) -> List[DeviceInfo]:  # pragma: no cover - abstract
        raise NotImplementedError

    def default_output(self) -> Optional[DeviceInfo]:  # pragma: no cover - abstract
        raise NotImplementedError

    def default_input(self) -> Optional[DeviceInfo]:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- sources ---------------------------------------------------------

    def create_system_source(self, device: Optional[str] = None) -> AudioSource:  # pragma: no cover
        raise NotImplementedError

    def create_microphone_source(self, device: Optional[str] = None) -> AudioSource:  # pragma: no cover
        raise NotImplementedError

    # -- permissions -----------------------------------------------------

    def check_permissions(self) -> List[PermissionReport]:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- description -----------------------------------------------------

    def describe_system_capture(self) -> str:
        """One line for the startup banner, e.g. 'Windows WASAPI Loopback'."""
        return self.name


class UnsupportedBackend(PlatformBackend):
    """Used when the platform has no supported audio system (spec 9, 20)."""

    def __init__(self, system: str, reason: str) -> None:
        self.name = system
        self.reason = reason

    def list_devices(self) -> List[DeviceInfo]:
        return []

    def default_output(self) -> Optional[DeviceInfo]:
        return None

    def default_input(self) -> Optional[DeviceInfo]:
        return None

    def check_permissions(self) -> List[PermissionReport]:
        return [
            PermissionReport("System audio", PermissionStatus.UNSUPPORTED,
                             self.reason,
                             "This environment has no supported system-audio "
                             "capture mechanism."),
            PermissionReport("Microphone", PermissionStatus.UNSUPPORTED, self.reason, ""),
        ]

    def create_system_source(self, device: Optional[str] = None) -> AudioSource:
        from .base import AudioSourceError
        raise AudioSourceError(self.reason, recoverable=False)

    def create_microphone_source(self, device: Optional[str] = None) -> AudioSource:
        from .base import AudioSourceError
        raise AudioSourceError(self.reason, recoverable=False)

    def describe_system_capture(self) -> str:
        return f"{self.name} (unsupported)"
