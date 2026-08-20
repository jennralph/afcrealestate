"""Behaviour that only matters in a packaged, double-clicked build.

A PyInstaller executable launched from Explorer or Finder gets a console
window that closes the instant the program returns — so an error message, or
the "recording finished" summary, would flash past unread.  It also starts
with the working directory wherever the file happens to live, which is a poor
place to write meetings to.

Nothing here changes what the recorder does; it only makes the packaged build
behave the way a double-clicked application should.  The zero-admin rules are
unaffected: a frozen build still requests no elevation and bypasses nothing.
"""

from __future__ import annotations

import os
import sys
from typing import Optional


def is_frozen() -> bool:
    """True when running from a PyInstaller (or similar) bundle."""
    return bool(getattr(sys, "frozen", False))


def owns_console() -> bool:
    """True when this process created the console window it is using.

    That is the double-clicked case on Windows: the window belongs to us and
    disappears when we exit.  Launched from an existing terminal — or on any
    other platform — this is False and nothing should pause.
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes

        # GetConsoleProcessList reports every process attached to this
        # console.  Just us means we created it.
        buffer = (ctypes.c_uint * 4)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(buffer, 4)
        return count <= 1
    except Exception:  # pragma: no cover - non-Windows or no console
        return False


def default_output_dir() -> str:
    """Where recordings go when ``--output`` was not given.

    A development checkout writes beside the source; a packaged build writes
    somewhere predictable in the user's home directory rather than into
    whichever folder the executable was dropped in.
    """
    if not is_frozen():
        return "./meetings"
    home = os.path.expanduser("~")
    documents = os.path.join(home, "Documents")
    base = documents if os.path.isdir(documents) else home
    return os.path.join(base, "MeetingCapture")


def hold_console(message: str = "Press ENTER to close this window. ") -> None:
    """Keep a double-clicked window open so its output can be read."""
    if not owns_console():
        return
    try:
        input("\n" + message)
    except (EOFError, KeyboardInterrupt, OSError):  # pragma: no cover - no stdin
        pass


def bundled_path(*parts: str) -> Optional[str]:
    """Locate a file bundled alongside the executable, if it is there."""
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        return None
    path = os.path.join(base, *parts)
    return path if os.path.exists(path) else None
