#!/usr/bin/env python3
"""Zero-admin meeting recorder — entry point (spec 3, 18).

    python meeting_capture.py

Captures system audio and the microphone as two independent tracks, follows
the user's audio environment across device changes, and never asks for
administrator or root privileges.

Dependencies are detected, never installed: if something is missing the
program prints one copyable command for an ordinary shell and exits.
"""

from __future__ import annotations

import sys


def _fail_without_dependencies() -> int:
    """Explain a missing numpy without a stack trace (spec 21)."""
    import platform

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


if __name__ == "__main__":
    sys.exit(main())
