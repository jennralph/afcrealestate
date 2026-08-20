"""Platform selection (spec 4).

``platform.system()`` chooses the backend here, once.  Everything above this
module is platform-neutral.
"""

from __future__ import annotations

import platform
from typing import Optional

from .backend import PlatformBackend, UnsupportedBackend

_cached: Optional[PlatformBackend] = None


def get_backend(name: Optional[str] = None, *, refresh: bool = False) -> PlatformBackend:
    """Return the backend for this machine, or the one named by *name*.

    ``name`` accepts ``auto`` (default), ``synthetic``, ``windows``,
    ``darwin``/``macos`` and ``linux`` — the explicit values exist for
    testing and for ``--source synthetic``.
    """
    global _cached
    if name in (None, "auto") and _cached is not None and not refresh:
        return _cached

    system = (name or platform.system()).strip().lower()
    if system in ("auto", ""):
        system = platform.system().lower()

    if system == "synthetic":
        from .synthetic import SyntheticBackend
        backend: PlatformBackend = SyntheticBackend()
    elif system == "windows":
        from .windows import WindowsBackend
        backend = WindowsBackend()
    elif system in ("darwin", "macos"):
        from .macos import MacBackend
        backend = MacBackend()
    elif system == "linux":
        from .linux import LinuxBackend
        linux_backend = LinuxBackend()
        backend = linux_backend
        if not linux_backend.available:
            # Keep the real backend: it still explains precisely what is
            # missing through check_permissions().
            backend = linux_backend
    else:
        backend = UnsupportedBackend(
            platform.system(),
            f"{platform.system()} is not a supported platform for audio capture.")

    if name in (None, "auto"):
        _cached = backend
    return backend


def reset_cache() -> None:
    """Forget the cached backend (used by tests and after a device storm)."""
    global _cached
    _cached = None
