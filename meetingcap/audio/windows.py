"""Windows capture via WASAPI shared-mode loopback (spec 5, 6).

Loopback taps the operating system's digital render stream before it reaches
speakers, headphones, USB, Bluetooth or HDMI.  It therefore does **not**
depend on Stereo Mix, "What U Hear", VB-Cable, VoiceMeeter, OBS virtual
devices or on the microphone acoustically hearing the speakers — and it
installs nothing, so no elevation is ever required.

Device names are never hard-coded: the current default render endpoint is
resolved at open time and re-resolved after every device change.
"""

from __future__ import annotations

import threading
from typing import Any, List, Optional

import numpy as np

from ..permissions import PermissionReport, PermissionStatus
from .backend import PlatformBackend
from .base import AudioSource, AudioSourceError, DeviceInfo

BLOCK_FRAMES = 480          # 10 ms at 48 kHz


def _import_pyaudio():
    try:
        import pyaudiowpatch as pyaudio  # type: ignore
    except ImportError as exc:   # pragma: no cover - Windows-only path
        raise AudioSourceError(
            "PyAudioWPatch is not installed; it is what exposes WASAPI "
            "loopback endpoints to Python.",
            recoverable=False) from exc
    return pyaudio


class _PyAudioStreamSource(AudioSource):
    """Shared plumbing for the two WASAPI streams.

    The PortAudio callback does one thing: copy the block into the bounded
    queue.  Everything else — resampling, level metering, disk — happens on
    other threads (spec 10).
    """

    def __init__(self, kind: str, *, prefer_device: Optional[str] = None) -> None:
        self.kind = kind
        super().__init__()
        self.prefer_device = prefer_device
        self._pa: Any = None
        self._stream: Any = None
        self._monitor: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self.sample_rate = 0
        self.channels = 0

    # -- to implement by subclass ---------------------------------------

    def _resolve_device(self, pa) -> dict:  # pragma: no cover - Windows-only
        raise NotImplementedError

    # -- lifecycle -------------------------------------------------------

    def _open(self) -> None:  # pragma: no cover - Windows-only path
        pyaudio = _import_pyaudio()
        self._stop_evt.clear()
        self._pa = pyaudio.PyAudio()
        try:
            info = self._resolve_device(self._pa)
        except AudioSourceError:
            self._pa.terminate()
            self._pa = None
            raise

        self.sample_rate = int(info.get("defaultSampleRate") or 48_000)
        self.channels = max(1, int(info.get("maxInputChannels") or 1))
        device_name = str(info.get("name", ""))
        self._set_device(device_name, self.sample_rate, self.channels)

        def callback(in_data, frame_count, time_info, status):
            # No allocation beyond the numpy view + copy; no I/O, no locks
            # held across anything slow.
            pcm = np.frombuffer(in_data, dtype=np.float32)
            if self.channels > 1:
                pcm = pcm.reshape(-1, self.channels)
            else:
                pcm = pcm.reshape(-1, 1)
            self._emit(np.array(pcm, dtype=np.float32), self.sample_rate,
                       self.channels, device_name)
            return (None, pyaudio.paContinue)

        try:
            self._stream = self._pa.open(
                format=pyaudio.paFloat32,
                channels=self.channels,
                rate=self.sample_rate,
                frames_per_buffer=BLOCK_FRAMES,
                input=True,
                input_device_index=int(info["index"]),
                stream_callback=callback,
            )
        except OSError as exc:
            self._pa.terminate()
            self._pa = None
            raise AudioSourceError(_explain_oserror(exc, self.kind)) from exc

        self._monitor = threading.Thread(target=self._watch_stream,
                                         name=f"wasapi-monitor-{self.kind.lower()}",
                                         daemon=True)
        self._monitor.start()

    def _watch_stream(self) -> None:  # pragma: no cover - Windows-only path
        """Detect AUDCLNT_E_DEVICE_INVALIDATED and friends.

        When Windows invalidates an endpoint (the headset was unplugged, the
        Bluetooth profile changed, the default moved) PortAudio stops the
        stream.  Reporting that as an error is what lets the watchdog reopen
        against the *new* endpoint without ending the meeting.
        """
        while not self._stop_evt.wait(0.25):
            stream = self._stream
            if stream is None:
                return
            try:
                if not stream.is_active():
                    self._set_error("WASAPI stream is no longer active "
                                    "(endpoint invalidated or device removed)")
                    return
            except OSError as exc:
                self._set_error(_explain_oserror(exc, self.kind))
                return

    def _close(self) -> None:  # pragma: no cover - Windows-only path
        self._stop_evt.set()
        monitor, self._monitor = self._monitor, None
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop_stream()
            except OSError:
                pass
            try:
                stream.close()
            except OSError:
                pass
        pa, self._pa = self._pa, None
        if pa is not None:
            try:
                pa.terminate()
            except OSError:
                pass
        if monitor is not None and monitor.is_alive():
            monitor.join(timeout=1.0)


class WindowsSystemAudio(_PyAudioStreamSource):
    """WASAPI shared-mode loopback on the current default render endpoint."""

    def __init__(self, prefer_device: Optional[str] = None) -> None:
        super().__init__("SYSTEM", prefer_device=prefer_device)

    def _resolve_device(self, pa) -> dict:  # pragma: no cover - Windows-only
        return resolve_loopback_device(pa, self.prefer_device)


class WindowsMicrophone(_PyAudioStreamSource):
    """The current default communication/capture endpoint."""

    def __init__(self, prefer_device: Optional[str] = None) -> None:
        super().__init__("MIC", prefer_device=prefer_device)

    def _resolve_device(self, pa) -> dict:  # pragma: no cover - Windows-only
        return resolve_input_device(pa, self.prefer_device)


# -- device resolution ---------------------------------------------------


def resolve_loopback_device(pa, prefer_device: Optional[str] = None) -> dict:
    """Find the loopback endpoint matching the current default output.

    Order of preference:

    1. an explicit ``--system`` name the user asked for;
    2. ``get_default_wasapi_loopback()`` where the installed PyAudioWPatch
       provides it;
    3. the loopback whose name matches the current default render device.
    """
    if prefer_device and prefer_device != "auto":
        for info in _loopback_devices(pa):
            if prefer_device.lower() in str(info.get("name", "")).lower():
                return info
        raise AudioSourceError(f"no WASAPI loopback endpoint matches {prefer_device!r}")

    getter = getattr(pa, "get_default_wasapi_loopback", None)
    if callable(getter):
        try:
            info = getter()
            if info:
                return dict(info)
        except (OSError, LookupError, ValueError):
            pass  # fall through to manual matching

    default_output = _default_render_device(pa)
    candidates = _loopback_devices(pa)
    if not candidates:
        raise AudioSourceError(
            "no WASAPI loopback endpoints are exposed on this system")
    if default_output:
        target = str(default_output.get("name", ""))
        for info in candidates:
            if str(info.get("name", "")).startswith(target):
                return info
    return candidates[0]


def resolve_input_device(pa, prefer_device: Optional[str] = None) -> dict:
    if prefer_device and prefer_device != "auto":
        for info in _all_devices(pa):
            if (int(info.get("maxInputChannels", 0)) > 0
                    and prefer_device.lower() in str(info.get("name", "")).lower()):
                return info
        raise AudioSourceError(f"no input device matches {prefer_device!r}")
    try:
        import pyaudiowpatch as pyaudio  # type: ignore
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        index = int(wasapi.get("defaultInputDevice", -1))
        if index >= 0:
            return dict(pa.get_device_info_by_index(index))
    except (ImportError, AttributeError, OSError, LookupError, ValueError):
        # AttributeError: the installed PyAudioWPatch predates the
        # WASAPI host-API helpers.  Fall back rather than crash.
        pass
    try:
        return dict(pa.get_default_input_device_info())
    except (OSError, LookupError, ValueError) as exc:
        raise AudioSourceError(
            "no default microphone is available. Connect a microphone or "
            "select one in Windows Sound settings.") from exc


def _default_render_device(pa) -> Optional[dict]:
    try:
        import pyaudiowpatch as pyaudio  # type: ignore
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        index = int(wasapi.get("defaultOutputDevice", -1))
        if index >= 0:
            return dict(pa.get_device_info_by_index(index))
    except (ImportError, AttributeError, OSError, LookupError, ValueError):
        # AttributeError: the installed PyAudioWPatch predates the
        # WASAPI host-API helpers.  Fall back rather than crash.
        pass
    try:
        return dict(pa.get_default_output_device_info())
    except (OSError, LookupError, ValueError):
        return None


def _loopback_devices(pa) -> List[dict]:
    generator = getattr(pa, "get_loopback_device_info_generator", None)
    if callable(generator):
        try:
            return [dict(info) for info in generator()]
        except (OSError, LookupError, ValueError):
            return []
    return [info for info in _all_devices(pa) if info.get("isLoopbackDevice")]


def _all_devices(pa) -> List[dict]:
    devices: List[dict] = []
    try:
        count = pa.get_device_count()
    except (OSError, LookupError, ValueError):
        return devices
    for index in range(count):
        try:
            devices.append(dict(pa.get_device_info_by_index(index)))
        except (OSError, LookupError, ValueError):  # pragma: no cover
            continue
    return devices


def _explain_oserror(exc: OSError, kind: str) -> str:
    text = str(exc)
    if "-9988" in text or "Invalid stream pointer" in text:
        return "the audio endpoint was invalidated by Windows"
    if "-9985" in text or "Device unavailable" in text:
        return "the audio device became unavailable"
    if "-9996" in text or "Invalid device" in text:
        return "the selected device index is no longer valid"
    return f"{kind} stream error: {text}"


# -- backend -------------------------------------------------------------


class WindowsBackend(PlatformBackend):
    name = "Windows"

    def __init__(self) -> None:
        self._pa: Any = None
        self._process_loopback_available: Optional[bool] = None

    # -- pyaudio handle --------------------------------------------------

    def _handle(self):  # pragma: no cover - Windows-only path
        pyaudio = _import_pyaudio()
        if self._pa is None:
            self._pa = pyaudio.PyAudio()
        return self._pa

    def _refresh(self):  # pragma: no cover - Windows-only path
        """Re-instantiate PortAudio so the device list is current.

        PortAudio caches enumeration at initialisation, so a long-lived
        handle would keep reporting the headset the user just unplugged.
        """
        if self._pa is not None:
            try:
                self._pa.terminate()
            except OSError:
                pass
            self._pa = None
        return self._handle()

    # -- enumeration -----------------------------------------------------

    def list_devices(self) -> List[DeviceInfo]:  # pragma: no cover - Windows-only
        pa = self._refresh()
        default_out = _default_render_device(pa) or {}
        devices: List[DeviceInfo] = []
        for info in _all_devices(pa):
            name = str(info.get("name", ""))
            if info.get("isLoopbackDevice"):
                kind = "loopback"
            elif int(info.get("maxInputChannels", 0)) > 0:
                kind = "input"
            else:
                kind = "output"
            devices.append(DeviceInfo(
                str(info.get("index")), name, kind,
                int(info.get("defaultSampleRate") or 0),
                int(info.get("maxInputChannels") or info.get("maxOutputChannels") or 0),
                is_default=(name == default_out.get("name")),
                host_api=str(info.get("hostApi", "")),
            ))
        return devices

    def default_output(self) -> Optional[DeviceInfo]:  # pragma: no cover - Windows-only
        pa = self._refresh()
        info = _default_render_device(pa)
        if not info:
            return None
        return DeviceInfo(str(info.get("index")), str(info.get("name", "")), "output",
                          int(info.get("defaultSampleRate") or 0),
                          int(info.get("maxOutputChannels") or 0), is_default=True)

    def default_input(self) -> Optional[DeviceInfo]:  # pragma: no cover - Windows-only
        pa = self._refresh()
        try:
            info = resolve_input_device(pa)
        except AudioSourceError:
            return None
        return DeviceInfo(str(info.get("index")), str(info.get("name", "")), "input",
                          int(info.get("defaultSampleRate") or 0),
                          int(info.get("maxInputChannels") or 0), is_default=True)

    # -- sources ---------------------------------------------------------

    def create_system_source(self, device: Optional[str] = None) -> AudioSource:
        from .windows_process_loopback import (WindowsProcessLoopback,
                                               process_loopback_supported)
        # Fallback hierarchy (spec 7): native process loopback where it is
        # both implemented and supported, then plain WASAPI loopback.
        if device in (None, "auto") and process_loopback_supported():
            try:
                return WindowsProcessLoopback()
            except AudioSourceError:
                pass
        return WindowsSystemAudio(prefer_device=device)

    def create_microphone_source(self, device: Optional[str] = None) -> AudioSource:
        return WindowsMicrophone(prefer_device=device)

    # -- permissions -----------------------------------------------------

    def check_permissions(self) -> List[PermissionReport]:  # pragma: no cover - Windows-only
        from ..deps import Dependency, install_command
        reports: List[PermissionReport] = []
        try:
            pa = self._refresh()
        except AudioSourceError as exc:
            dep = Dependency("pyaudiowpatch", "PyAudioWPatch", "WASAPI loopback")
            command = install_command([dep], "Windows")
            return [
                PermissionReport("System audio", PermissionStatus.DEPENDENCY_MISSING,
                                 str(exc), f"Install it with:\n  {command}"),
                PermissionReport("Microphone", PermissionStatus.DEPENDENCY_MISSING,
                                 str(exc), f"Install it with:\n  {command}"),
            ]
        try:
            info = resolve_loopback_device(pa)
            reports.append(PermissionReport(
                "System audio", PermissionStatus.SUPPORTED_AND_ALLOWED,
                f"WASAPI loopback: {info.get('name', '')}", ""))
        except AudioSourceError as exc:
            reports.append(PermissionReport(
                "System audio", PermissionStatus.DEVICE_UNAVAILABLE, str(exc),
                "Select an output device in Windows Sound settings and try again."))
        try:
            info = resolve_input_device(pa)
            reports.append(PermissionReport(
                "Microphone", PermissionStatus.SUPPORTED_AND_ALLOWED,
                f"Default capture endpoint: {info.get('name', '')}", ""))
        except AudioSourceError as exc:
            reports.append(PermissionReport(
                "Microphone", PermissionStatus.SUPPORTED_BUT_PERMISSION_MISSING, str(exc),
                "Windows may be blocking microphone access for desktop apps.\n"
                "Open: Settings -> Privacy & security -> Microphone\n"
                "and allow desktop apps to access the microphone."))
        return reports

    def describe_system_capture(self) -> str:
        return "Windows WASAPI Loopback"
