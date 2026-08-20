"""macOS capture via Apple's native ScreenCaptureKit (spec 8).

No virtual driver is installed — not BlackHole, Loopback or Soundflower.
System audio comes from ScreenCaptureKit with ``capturesAudio = true``; the
application wants audio, not pixels, so any video frames the framework
insists on producing are discarded.

Two paths are provided, in the order the spec suggests:

1. a small Swift helper (``helpers/macos/SystemAudioCapture.swift``) that
   streams raw float32 on stdout — the reliable path for continuous capture,
   and the default when it has been built;
2. a direct PyObjC path, experimental, used when no helper is present.

macOS will require *Screen & System Audio Recording* and *Microphone*
permissions.  Those are legitimate OS privacy controls: this module detects
their state and tells the user where to grant them.  It never attempts to
modify TCC or work around a denial.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from typing import Any, List, Optional

import numpy as np

from ..permissions import PermissionReport, PermissionStatus
from .backend import PlatformBackend
from .base import AudioSource, AudioSourceError, DeviceInfo

HELPER_ENV_VAR = "MEETINGCAP_MACOS_HELPER"
DEFAULT_HELPER_NAME = "meetingcap-system-audio"
TARGET_RATE = 48_000
BLOCK_FRAMES = 480

SCREEN_PERMISSION_REMEDY = (
    "Open: System Settings -> Privacy & Security -> Screen & System Audio Recording\n"
    "Enable it for the application running this recorder (Terminal, iTerm, or\n"
    "your packaged app), then restart the application.")
MIC_PERMISSION_REMEDY = (
    "Open: System Settings -> Privacy & Security -> Microphone\n"
    "Enable it for the application running this recorder, then restart it.")


def helper_path() -> Optional[str]:
    configured = os.environ.get(HELPER_ENV_VAR)
    if configured and os.path.exists(configured):
        return configured
    found = shutil.which(DEFAULT_HELPER_NAME)
    if found:
        return found
    local = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..",
        "helpers", "macos", DEFAULT_HELPER_NAME))
    return local if os.path.exists(local) else None


# -- permission probes ---------------------------------------------------


def screen_recording_allowed() -> Optional[bool]:
    """``True``/``False`` from CoreGraphics, ``None`` if it cannot be probed.

    ``CGPreflightScreenCaptureAccess`` asks without prompting, which is what
    lets the startup banner state the situation up front.
    """
    try:
        import Quartz  # type: ignore
    except ImportError:
        return None
    preflight = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
    if not callable(preflight):
        return None
    try:
        return bool(preflight())
    except Exception:  # pragma: no cover - framework quirk
        return None


def microphone_allowed() -> Optional[bool]:
    try:
        import AVFoundation  # type: ignore
    except ImportError:
        return None
    try:
        status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
            AVFoundation.AVMediaTypeAudio)
    except Exception:  # pragma: no cover - framework quirk
        return None
    # 0 notDetermined, 1 restricted, 2 denied, 3 authorized
    if status == 3:
        return True
    if status == 0:
        return None
    return False


# -- system audio --------------------------------------------------------


class MacSystemAudioHelper(AudioSource):
    """ScreenCaptureKit capture through the Swift helper process."""

    kind = "SYSTEM"

    def __init__(self, *, channels: int = 2) -> None:
        super().__init__()
        self.channels = channels
        self.sample_rate = TARGET_RATE
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._set_device("ScreenCaptureKit (system audio)", TARGET_RATE, channels)

    def _open(self) -> None:  # pragma: no cover - macOS-only path
        helper = helper_path()
        if helper is None:
            raise AudioSourceError("macOS system-audio helper is not built",
                                   recoverable=False)
        argv = [helper, "--rate", str(TARGET_RATE), "--channels", str(self.channels)]
        self._stop_evt.clear()
        try:
            self._proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, bufsize=0)
        except OSError as exc:
            raise AudioSourceError(f"could not start the macOS helper: {exc}") from exc
        self._thread = threading.Thread(target=self._pump, name="mac-system-audio",
                                        daemon=True)
        self._thread.start()

    def _pump(self) -> None:  # pragma: no cover - macOS-only path
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
                    stderr = b""
                    if proc.stderr is not None:
                        try:
                            stderr = proc.stderr.read() or b""
                        except (OSError, ValueError):
                            pass
                    message = stderr.decode("utf-8", "replace").strip()
                    if "permission" in message.lower() or "TCC" in message:
                        self._set_error(
                            "Screen & System Audio Recording permission is not granted")
                    else:
                        self._set_error(f"macOS helper exited{': ' + message if message else ''}")
                return
            buffer += data
            usable = len(buffer) - (len(buffer) % bytes_per_frame)
            if usable <= 0:
                continue
            frames = np.frombuffer(buffer[:usable], dtype="<f4").reshape(-1, self.channels)
            buffer = buffer[usable:]
            self._emit(np.array(frames, dtype=np.float32), self.sample_rate,
                       self.channels, "ScreenCaptureKit")

    def _close(self) -> None:  # pragma: no cover - macOS-only path
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


class MacSystemAudioPyObjC(AudioSource):
    """Experimental in-process ScreenCaptureKit capture.

    Kept behind the helper because continuous ``CMSampleBuffer`` handling
    from Python has proven fragile; per spec 8, reliability outranks keeping
    everything in one language.
    """

    kind = "SYSTEM"

    def __init__(self, *, channels: int = 2) -> None:
        super().__init__()
        self.channels = channels
        self.sample_rate = TARGET_RATE
        self._stream: Any = None
        self._set_device("ScreenCaptureKit (PyObjC)", TARGET_RATE, channels)

    def _open(self) -> None:  # pragma: no cover - macOS-only path
        try:
            import ScreenCaptureKit  # type: ignore  # noqa: F401
        except ImportError as exc:
            raise AudioSourceError(
                "pyobjc-framework-ScreenCaptureKit is not installed",
                recoverable=False) from exc
        if screen_recording_allowed() is False:
            raise AudioSourceError(
                "Screen & System Audio Recording permission is not granted",
                recoverable=False)
        raise AudioSourceError(
            "in-process ScreenCaptureKit capture is not enabled in this build; "
            "build the Swift helper (helpers/macos/README.md) for system audio",
            recoverable=False)

    def _close(self) -> None:  # pragma: no cover - macOS-only path
        self._stream = None


# -- microphone ----------------------------------------------------------


class MacMicrophone(AudioSource):
    """Microphone capture through PortAudio (``sounddevice``)."""

    kind = "MIC"

    def __init__(self, prefer_device: Optional[str] = None, *, channels: int = 1) -> None:
        super().__init__()
        self.prefer_device = prefer_device
        self.channels = channels
        self.sample_rate = TARGET_RATE
        self._stream: Any = None

    def _open(self) -> None:  # pragma: no cover - macOS-only path
        try:
            import sounddevice as sd  # type: ignore
        except ImportError as exc:
            raise AudioSourceError("sounddevice is not installed",
                                   recoverable=False) from exc
        if microphone_allowed() is False:
            raise AudioSourceError("Microphone permission is not granted",
                                   recoverable=False)
        device = None
        name = "default microphone"
        if self.prefer_device and self.prefer_device != "auto":
            device = self.prefer_device
            name = self.prefer_device
        else:
            try:
                info = sd.query_devices(kind="input")
                name = str(info.get("name", name))
            except Exception:
                pass
        self._set_device(name, TARGET_RATE, self.channels)

        def callback(indata, frames, time_info, status):
            self._emit(np.array(indata, dtype=np.float32), self.sample_rate,
                       self.channels, name)

        try:
            self._stream = sd.InputStream(
                samplerate=TARGET_RATE, channels=self.channels, dtype="float32",
                blocksize=BLOCK_FRAMES, device=device, callback=callback)
            self._stream.start()
        except Exception as exc:
            raise AudioSourceError(f"could not open the microphone: {exc}") from exc

    def _close(self) -> None:  # pragma: no cover - macOS-only path
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass


# -- backend -------------------------------------------------------------


class MacBackend(PlatformBackend):
    name = "macOS"

    # ScreenCaptureKit taps the system mix, not a particular endpoint, so
    # switching from speakers to AirPods does not require a reopen.
    system_capture_follows_default_endpoint = False

    def list_devices(self) -> List[DeviceInfo]:  # pragma: no cover - macOS-only
        devices = [DeviceInfo("sck", "ScreenCaptureKit system audio", "loopback",
                              TARGET_RATE, 2, is_default=True, host_api="ScreenCaptureKit")]
        try:
            import sounddevice as sd  # type: ignore
            default_in = sd.default.device[0]
            for index, info in enumerate(sd.query_devices()):
                if int(info.get("max_input_channels", 0)) <= 0:
                    continue
                devices.append(DeviceInfo(
                    str(index), str(info.get("name", "")), "input",
                    int(info.get("default_samplerate") or 0),
                    int(info.get("max_input_channels") or 0),
                    is_default=(index == default_in), host_api="CoreAudio"))
        except Exception:
            pass
        return devices

    def default_output(self) -> Optional[DeviceInfo]:  # pragma: no cover - macOS-only
        # System capture is endpoint-independent on macOS: ScreenCaptureKit
        # taps the mix, so a change of output device does not require a
        # reopen.  The current endpoint is still reported for the UI.
        name = "System audio (ScreenCaptureKit)"
        try:
            import sounddevice as sd  # type: ignore
            info = sd.query_devices(kind="output")
            name = str(info.get("name", name))
        except Exception:
            pass
        return DeviceInfo("sck", name, "output", TARGET_RATE, 2, is_default=True)

    def default_input(self) -> Optional[DeviceInfo]:  # pragma: no cover - macOS-only
        try:
            import sounddevice as sd  # type: ignore
            info = sd.query_devices(kind="input")
            return DeviceInfo(str(sd.default.device[0]), str(info.get("name", "")),
                              "input", int(info.get("default_samplerate") or TARGET_RATE),
                              int(info.get("max_input_channels") or 1), is_default=True)
        except Exception:
            return None

    def create_system_source(self, device: Optional[str] = None) -> AudioSource:
        if helper_path() is not None:
            return MacSystemAudioHelper()
        return MacSystemAudioPyObjC()

    def create_microphone_source(self, device: Optional[str] = None) -> AudioSource:
        return MacMicrophone(prefer_device=device)

    def check_permissions(self) -> List[PermissionReport]:
        reports: List[PermissionReport] = []
        screen = screen_recording_allowed()
        if screen is True:
            detail = ("ScreenCaptureKit helper: " + str(helper_path())
                      if helper_path() else
                      "permission granted, but the Swift helper is not built")
            status = (PermissionStatus.SUPPORTED_AND_ALLOWED if helper_path()
                      else PermissionStatus.UNSUPPORTED)
            remedy = ("" if helper_path() else
                      "Build it: see helpers/macos/README.md (no administrator "
                      "password required).")
            reports.append(PermissionReport("System audio", status, detail, remedy))
        elif screen is False:
            reports.append(PermissionReport(
                "System audio", PermissionStatus.SUPPORTED_BUT_PERMISSION_MISSING,
                "System audio capture is supported, but macOS has not granted "
                "Screen & System Audio Recording permission.",
                SCREEN_PERMISSION_REMEDY))
        else:
            reports.append(PermissionReport(
                "System audio", PermissionStatus.SUPPORTED_BUT_PERMISSION_MISSING,
                "Screen & System Audio Recording permission state is unknown; "
                "macOS will prompt on first capture.",
                SCREEN_PERMISSION_REMEDY))

        mic = microphone_allowed()
        if mic is True:
            reports.append(PermissionReport(
                "Microphone", PermissionStatus.SUPPORTED_AND_ALLOWED,
                "Microphone permission granted", ""))
        elif mic is False:
            reports.append(PermissionReport(
                "Microphone", PermissionStatus.SUPPORTED_BUT_PERMISSION_MISSING,
                "Microphone capture is supported, but macOS has not granted "
                "Microphone permission.", MIC_PERMISSION_REMEDY))
        else:
            reports.append(PermissionReport(
                "Microphone", PermissionStatus.SUPPORTED_BUT_PERMISSION_MISSING,
                "Microphone permission state is unknown; macOS will prompt on "
                "first capture.", MIC_PERMISSION_REMEDY))
        return reports

    def describe_system_capture(self) -> str:
        return "macOS ScreenCaptureKit"
