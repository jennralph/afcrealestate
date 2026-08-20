"""Dependency detection without surprise installation (spec 21).

The program never invokes pip on the user's behalf.  It imports what it
needs, and when something is missing it prints a single copyable command for
an ordinary, non-administrator shell.
"""

from __future__ import annotations

import importlib.util
import platform
import sys
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class Dependency:
    import_name: str
    pip_name: str
    purpose: str
    platforms: tuple = ()      # empty means every platform
    required: bool = True

    def applies(self, system: Optional[str] = None) -> bool:
        system = system or platform.system()
        return not self.platforms or system in self.platforms

    @property
    def installed(self) -> bool:
        try:
            return importlib.util.find_spec(self.import_name) is not None
        except (ImportError, ValueError):  # pragma: no cover - broken namespace pkg
            return False


DEPENDENCIES: List[Dependency] = [
    Dependency("numpy", "numpy", "internal float32 audio buffers"),
    Dependency("pyaudiowpatch", "PyAudioWPatch",
               "WASAPI shared-mode loopback + microphone", ("Windows",)),
    Dependency("sounddevice", "sounddevice",
               "microphone capture via PortAudio", ("Darwin",)),
    Dependency("ScreenCaptureKit", "pyobjc-framework-ScreenCaptureKit",
               "native macOS system-audio capture", ("Darwin",), required=False),
    Dependency("AVFoundation", "pyobjc-framework-AVFoundation",
               "macOS microphone permission state", ("Darwin",), required=False),
]


def missing(system: Optional[str] = None, *, required_only: bool = True) -> List[Dependency]:
    """Dependencies that apply to this platform but are not importable."""
    return [d for d in DEPENDENCIES
            if d.applies(system) and (d.required or not required_only) and not d.installed]


def install_command(deps: List[Dependency], system: Optional[str] = None) -> str:
    """A single copyable, non-administrator install command."""
    system = system or platform.system()
    names = " ".join(sorted({d.pip_name for d in deps}))
    if not names:
        return ""
    if system == "Windows":
        return f"py -m pip install {names}"
    return f"{_python()} -m pip install {names}"


def setup_instructions(system: Optional[str] = None) -> str:
    """Full venv + install text for the platform (spec 21 example)."""
    system = system or platform.system()
    deps = [d for d in DEPENDENCIES if d.applies(system) and d.required]
    names = " ".join(sorted({d.pip_name for d in deps}))
    if system == "Windows":
        return (f"py -m venv .venv\n"
                f".venv\\Scripts\\python -m pip install {names}\n"
                f"\nNo administrator shell is required.")
    return (f"python3 -m venv .venv\n"
            f".venv/bin/python -m pip install {names}\n"
            f"\nNo sudo is required.")


def report(system: Optional[str] = None) -> str:
    """Human-readable dependency status, empty when everything is present."""
    system = system or platform.system()
    gaps = missing(system)
    if not gaps:
        return ""
    lines = ["Missing dependencies:"]
    for dep in gaps:
        lines.append(f"  - {dep.pip_name}: {dep.purpose}")
    lines.append("")
    lines.append("Install them with:")
    lines.append(f"  {install_command(gaps, system)}")
    return "\n".join(lines)


def _python() -> str:
    if getattr(sys, "frozen", False):
        # sys.executable is the bundled app, not an interpreter that could
        # run pip; name the command a user would actually type.
        return "python3"
    return sys.executable or "python3"
