#!/usr/bin/env python3
"""Zero-admin meeting recorder — entry point (spec 3, 18).

    python meeting_capture.py

Captures system audio and the microphone as two independent tracks, follows
the user's audio environment across device changes, and never asks for
administrator or root privileges.

This is also the entry point of the packaged (PyInstaller) build, so it takes
care of the double-click case: a console window that would otherwise close
before anything could be read is held open, and an unexpected failure is
reported as a message rather than a vanishing traceback.

Dependencies are detected, never installed: if something is missing the
program prints one copyable command and exits.
"""

from __future__ import annotations

import sys


def _fail_without_dependencies() -> int:
    """Explain a missing numpy without a stack trace (spec 21)."""
    import platform

    if getattr(sys, "frozen", False):
        # Should be impossible: a packaged build bundles its dependencies.
        print("This build is missing numpy, which should have been bundled.")
        print("Please report this build as broken.")
        return 2

    if platform.system() == "Windows":
        command = "py -m pip install numpy PyAudioWPatch"
    elif platform.system() == "Darwin":
        command = f"{sys.executable} -m pip install numpy sounddevice"
    else:
        command = f"{sys.executable} -m pip install numpy"
    print("Missing dependency: numpy (internal float32 audio buffers)")
    print()
    print("Install it with:")
    print(f"  {command}")
    print()
    print("No administrator shell is required.")
    return 2


def main() -> int:
    try:
        import numpy  # noqa: F401
    except ImportError:
        return _fail_without_dependencies()

    from meetingcap.cli import main as cli_main

    return cli_main()


def _run() -> int:
    """Wrap :func:`main` so a packaged build never flashes and disappears."""
    from meetingcap.frozen import hold_console

    try:
        code = main()
    except KeyboardInterrupt:
        print("\nStopped.")
        code = 130
    except Exception as exc:  # noqa: BLE001 - last resort for a GUI launch
        print("\nThe recorder stopped because of an unexpected error:")
        print(f"  {type(exc).__name__}: {exc}")
        print("\nRun with --diagnostics for a capture self-test.")
        hold_console()
        raise
    hold_console()
    return code


if __name__ == "__main__":
    sys.exit(_run())
