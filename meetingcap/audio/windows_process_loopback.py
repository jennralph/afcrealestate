"""Process loopback on Windows (spec 7) — optional, never an MVP prerequisite.

``ActivateAudioInterfaceAsync`` with ``VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK``
provides loopback that is tied to a *process tree* rather than to a physical
render endpoint.  Used in exclude mode ("capture everything except our own
process tree") it survives endpoint changes and avoids feeding the recorder's
own audio back into the capture.

Only documented Windows APIs are involved and no elevation is required.  The
activation call itself is COM/WinRT and is not usefully expressible in
ctypes, so it lives in a small native helper that streams raw float32 on
stdout.  When the helper is absent — the normal case — this module reports
that clearly and :class:`~meetingcap.audio.windows.WindowsBackend` falls
through the hierarchy in spec 7:

1. native process loopback (here, if implemented and supported)
2. default WASAPI loopback
3. re-discovered WASAPI output endpoint
4. graceful error

Helper contract (so it can be supplied independently of this package):

* invoked as ``<helper> --exclude-pid <pid> --rate 48000 --channels 2 --format f32``
* writes interleaved little-endian float32 frames to stdout, nothing else
* writes diagnostics to stderr
* exits non-zero if activation fails

Set ``MEETINGCAP_PROCESS_LOOPBACK_HELPER`` to its path to enable it.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import threading
from typing import List, Optional

import numpy as np

from .base import AudioSource, AudioSourceError

HELPER_ENV_VAR = "MEETINGCAP_PROCESS_LOOPBACK_HELPER"
DEFAULT_HELPER_NAME = "meetingcap-process-loopback.exe"

#: Process loopback landed in Windows 10 build 20348 / Windows 11.
MIN_BUILD = 20348

TARGET_RATE = 48_000
BLOCK_FRAMES = 480


def helper_path() -> Optional[str]:
    """Locate the native helper, if the deployment ships one."""
    configured = os.environ.get(HELPER_ENV_VAR)
    if configured and os.path.exists(configured):
        return configured
    found = shutil.which(DEFAULT_HELPER_NAME)
    if found:
        return found
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "..", "helpers", "windows", DEFAULT_HELPER_NAME)
    local = os.path.normpath(local)
    return local if os.path.exists(local) else None


def windows_build() -> int:
    """Current Windows build number, or 0 when it cannot be determined."""
    if platform.system() != "Windows":
        return 0
    release = platform.win32_ver()[1] if hasattr(platform, "win32_ver") else ""
    parts = str(release).split(".")
    try:
        return int(parts[-1])
    except (ValueError, IndexError):  # pragma: no cover - odd version strings
        return 0


def process_loopback_supported() -> bool:
    """True only when the OS supports it *and* a helper is available."""
    return (platform.system() == "Windows"
            and windows_build() >= MIN_BUILD
            and helper_path() is not None)


def support_status() -> str:
    """One line for ``--diagnostics``."""
    if platform.system() != "Windows":
        return "not applicable (not Windows)"
    build = windows_build()
    if build and build < MIN_BUILD:
        return f"unsupported on build {build} (requires {MIN_BUILD}+)"
    if helper_path() is None:
        return (f"supported by the OS, helper not installed "
                f"(set {HELPER_ENV_VAR} to enable) — using WASAPI loopback")
    return f"available via {helper_path()}"


class WindowsProcessLoopback(AudioSource):
    """Process-tree loopback in exclude mode, via the native helper."""

    kind = "SYSTEM"

    def __init__(self, *, exclude_pid: Optional[int] = None, channels: int = 2) -> None:
        super().__init__()
        self.exclude_pid = exclude_pid if exclude_pid is not None else os.getpid()
        self.channels = channels
        self.sample_rate = TARGET_RATE
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._set_device("Windows process loopback (exclude self)",
                         TARGET_RATE, channels)

    def _argv(self) -> List[str]:
        helper = helper_path()
        if helper is None:
            raise AudioSourceError(
                "process loopback helper is not installed", recoverable=False)
        return [helper, "--exclude-pid", str(self.exclude_pid),
                "--rate", str(TARGET_RATE), "--channels", str(self.channels),
                "--format", "f32"]

    def _open(self) -> None:  # pragma: no cover - Windows-only path
        if windows_build() and windows_build() < MIN_BUILD:
            raise AudioSourceError(
                f"process loopback requires Windows build {MIN_BUILD} or newer",
                recoverable=False)
        argv = self._argv()
        self._stop_evt.clear()
        try:
            self._proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, bufsize=0)
        except OSError as exc:
            raise AudioSourceError(f"could not start process-loopback helper: {exc}") from exc
        self._thread = threading.Thread(target=self._pump, name="process-loopback",
                                        daemon=True)
        self._thread.start()

    def _pump(self) -> None:  # pragma: no cover - Windows-only path
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        bytes_per_frame = 4 * self.channels
        block_bytes = BLOCK_FRAMES * bytes_per_frame
        buffer = b""
        while not self._stop_evt.is_set():
            data = proc.stdout.read(block_bytes)
            if not data:
                if not self._stop_evt.is_set():
                    self._set_error("process-loopback helper exited")
                return
            buffer += data
            usable = len(buffer) - (len(buffer) % bytes_per_frame)
            if usable <= 0:
                continue
            frames = np.frombuffer(buffer[:usable], dtype="<f4").reshape(-1, self.channels)
            buffer = buffer[usable:]
            self._emit(np.array(frames, dtype=np.float32), self.sample_rate,
                       self.channels, "Windows process loopback")

    def _close(self) -> None:  # pragma: no cover - Windows-only path
        self._stop_evt.set()
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
