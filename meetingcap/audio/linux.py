"""Linux capture: PipeWire first, PulseAudio as a fallback (spec 9).

System audio comes from the *monitor* of the currently active sink, which is
the operating system's render stream — not an acoustic pickup.  The active
sink is discovered dynamically and re-discovered whenever the default node
changes, so switching from speakers to a Bluetooth headset mid-meeting is a
reopen, not a failure.

Capture runs through the tools that ship with the audio server itself
(``pw-record`` / ``parec``) streaming raw float32 on stdout.  Nothing is
installed, no system packages are touched, and no elevation is involved.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..permissions import PermissionReport, PermissionStatus
from .backend import PlatformBackend
from .base import AudioSource, AudioSourceError, DeviceInfo

TARGET_RATE = 48_000
BLOCK_FRAMES = 480          # 10 ms
_TIMEOUT = 3.0


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _run(args: List[str], timeout: float = _TIMEOUT) -> Optional[str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


class _SubprocessSource(AudioSource):
    """Reads raw float32 frames from a capture helper's stdout."""

    def __init__(self, kind: str, argv: List[str], *, sample_rate: int,
                 channels: int, device: str) -> None:
        self.kind = kind
        super().__init__()
        self.argv = argv
        self.sample_rate = sample_rate
        self.channels = channels
        self.device = device
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._set_device(device, sample_rate, channels)

    def _open(self) -> None:
        self._stop_evt.clear()
        try:
            self._proc = subprocess.Popen(
                self.argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0)
        except OSError as exc:
            raise AudioSourceError(f"could not start {self.argv[0]}: {exc}") from exc
        self._thread = threading.Thread(target=self._pump, name=f"linux-{self.kind.lower()}",
                                        daemon=True)
        self._thread.start()

    def _close(self) -> None:
        self._stop_evt.set()
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except (OSError, subprocess.TimeoutExpired):  # pragma: no cover
                try:
                    proc.kill()
                except OSError:
                    pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _pump(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:  # pragma: no cover - defensive
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
                        except (OSError, ValueError):  # pragma: no cover
                            pass
                    detail = stderr.decode("utf-8", "replace").strip().splitlines()
                    self._set_error(
                        f"{self.argv[0]} exited"
                        + (f": {detail[-1]}" if detail else ""))
                return
            buffer += data
            usable = len(buffer) - (len(buffer) % bytes_per_frame)
            if usable <= 0:
                continue
            frames = np.frombuffer(buffer[:usable], dtype="<f4").reshape(-1, self.channels)
            buffer = buffer[usable:]
            # Copy: the buffer we sliced from is about to be rebound.
            self._emit(np.array(frames, dtype=np.float32), self.sample_rate,
                       self.channels, self.device)


class LinuxSystemAudio(_SubprocessSource):
    """Captures the monitor of the active sink."""

    def __init__(self, monitor: str, *, channels: int = 2, tool: str = "parec",
                 device_label: str = "") -> None:
        argv = _capture_argv(tool, monitor, channels)
        super().__init__("SYSTEM", argv, sample_rate=TARGET_RATE, channels=channels,
                         device=device_label or monitor)
        self.monitor = monitor


class LinuxMicrophone(_SubprocessSource):
    """Captures the active capture source."""

    def __init__(self, source: str, *, channels: int = 1, tool: str = "parec",
                 device_label: str = "") -> None:
        argv = _capture_argv(tool, source, channels)
        super().__init__("MIC", argv, sample_rate=TARGET_RATE, channels=channels,
                         device=device_label or source)


def _capture_argv(tool: str, target: str, channels: int) -> List[str]:
    if tool == "pw-record":
        return ["pw-record", "--target", target, "--rate", str(TARGET_RATE),
                "--channels", str(channels), "--format", "f32", "-"]
    return ["parec", f"--device={target}", "--format=float32le",
            f"--rate={TARGET_RATE}", f"--channels={channels}",
            "--latency-msec=20", "--raw"]


class LinuxBackend(PlatformBackend):
    """PipeWire/PulseAudio device discovery and source construction."""

    name = "Linux"

    def __init__(self) -> None:
        self.has_pactl = _which("pactl") is not None
        self.has_parec = _which("parec") is not None
        self.has_pw_record = _which("pw-record") is not None
        self.has_pw_dump = _which("pw-dump") is not None
        self.server = self._detect_server()

    # -- server detection ------------------------------------------------

    def _detect_server(self) -> str:
        info = _run(["pactl", "info"]) if self.has_pactl else None
        if info:
            if "PipeWire" in info:
                return "PipeWire"
            return "PulseAudio"
        if self.has_pw_dump or self.has_pw_record:
            return "PipeWire"
        return "none"

    @property
    def available(self) -> bool:
        return self.server != "none" and (self.has_parec or self.has_pw_record)

    @property
    def tool(self) -> str:
        # parec speaks to both servers (pipewire-pulse provides it), so it is
        # preferred for uniformity; pw-record is used on a PipeWire box that
        # ships no Pulse compatibility layer.
        if self.has_parec:
            return "parec"
        if self.has_pw_record:
            return "pw-record"
        return ""

    # -- enumeration -----------------------------------------------------

    def default_sink_name(self) -> Optional[str]:
        out = _run(["pactl", "get-default-sink"]) if self.has_pactl else None
        if out and out.strip():
            return out.strip()
        return self._pw_default("default.audio.sink")

    def default_source_name(self) -> Optional[str]:
        out = _run(["pactl", "get-default-source"]) if self.has_pactl else None
        if out and out.strip():
            return out.strip()
        return self._pw_default("default.audio.source")

    def _pw_default(self, key: str) -> Optional[str]:
        if not self.has_pw_dump:
            return None
        raw = _run(["pw-dump"])
        if not raw:
            return None
        try:
            nodes = json.loads(raw)
        except json.JSONDecodeError:  # pragma: no cover - malformed dump
            return None
        for node in nodes:
            if node.get("type") != "PipeWire:Interface:Metadata":
                continue
            for entry in node.get("metadata", []) or []:
                if entry.get("key") == key:
                    value = entry.get("value")
                    if isinstance(value, dict):
                        return value.get("name")
                    return value
        return None

    def _sinks(self) -> List[Tuple[str, str, int, int]]:
        return self._short_list("sinks")

    def _sources(self) -> List[Tuple[str, str, int, int]]:
        return self._short_list("sources")

    def _short_list(self, what: str) -> List[Tuple[str, str, int, int]]:
        """(index, name, channels, rate) from ``pactl list short``."""
        out = _run(["pactl", "list", "short", what]) if self.has_pactl else None
        entries: List[Tuple[str, str, int, int]] = []
        if not out:
            return entries
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            index, name = parts[0], parts[1]
            channels, rate = 2, TARGET_RATE
            if len(parts) >= 4:
                spec = parts[3]           # e.g. s16le 2ch 48000Hz
                for token in spec.split():
                    if token.endswith("ch") and token[:-2].isdigit():
                        channels = int(token[:-2])
                    elif token.endswith("Hz") and token[:-2].isdigit():
                        rate = int(token[:-2])
            entries.append((index, name, channels, rate))
        return entries

    def list_devices(self) -> List[DeviceInfo]:
        default_sink = self.default_sink_name()
        default_source = self.default_source_name()
        devices: List[DeviceInfo] = []
        for index, name, channels, rate in self._sinks():
            devices.append(DeviceInfo(index, name, "output", rate, channels,
                                      is_default=(name == default_sink),
                                      host_api=self.server))
            devices.append(DeviceInfo(f"{index}-monitor", f"{name}.monitor", "loopback",
                                      rate, channels,
                                      is_default=(name == default_sink),
                                      host_api=self.server))
        for index, name, channels, rate in self._sources():
            if name.endswith(".monitor"):
                continue
            devices.append(DeviceInfo(index, name, "input", rate, min(channels, 2),
                                      is_default=(name == default_source),
                                      host_api=self.server))
        return devices

    def default_output(self) -> Optional[DeviceInfo]:
        name = self.default_sink_name()
        if not name:
            return None
        for index, sink, channels, rate in self._sinks():
            if sink == name:
                return DeviceInfo(index, name, "output", rate, channels, is_default=True,
                                  host_api=self.server,
                                  extra={"monitor": f"{name}.monitor"})
        return DeviceInfo("?", name, "output", TARGET_RATE, 2, is_default=True,
                          host_api=self.server, extra={"monitor": f"{name}.monitor"})

    def default_input(self) -> Optional[DeviceInfo]:
        name = self.default_source_name()
        if not name:
            return None
        for index, source, channels, rate in self._sources():
            if source == name:
                return DeviceInfo(index, name, "input", rate, min(channels, 2),
                                  is_default=True, host_api=self.server)
        return DeviceInfo("?", name, "input", TARGET_RATE, 1, is_default=True,
                          host_api=self.server)

    # -- sources ---------------------------------------------------------

    def create_system_source(self, device: Optional[str] = None) -> AudioSource:
        if not self.available:
            raise AudioSourceError(self._unsupported_message(), recoverable=False)
        if device and device != "auto":
            monitor = device if device.endswith(".monitor") else f"{device}.monitor"
            channels = 2
        else:
            sink = self.default_output()
            if sink is None:
                raise AudioSourceError("no active PipeWire/PulseAudio sink found")
            monitor = sink.extra.get("monitor", f"{sink.name}.monitor")
            channels = min(2, sink.channels or 2)
        return LinuxSystemAudio(monitor, channels=channels, tool=self.tool,
                                device_label=monitor)

    def create_microphone_source(self, device: Optional[str] = None) -> AudioSource:
        if not self.available:
            raise AudioSourceError(self._unsupported_message(), recoverable=False)
        if device and device != "auto":
            target = device
        else:
            info = self.default_input()
            if info is None:
                raise AudioSourceError("no active capture source found")
            target = info.name
        return LinuxMicrophone(target, channels=1, tool=self.tool, device_label=target)

    # -- permissions -----------------------------------------------------

    def check_permissions(self) -> List[PermissionReport]:
        if not self.available:
            message = self._unsupported_message()
            return [
                PermissionReport("System audio", PermissionStatus.UNSUPPORTED, message,
                                 "Install and start PipeWire (recommended) or "
                                 "PulseAudio using your distribution's package "
                                 "manager, then run this program again.\n"
                                 "This application does not install system "
                                 "packages for you."),
                PermissionReport("Microphone", PermissionStatus.UNSUPPORTED, message, ""),
            ]
        reports: List[PermissionReport] = []
        sink = self.default_output()
        if sink is None:
            reports.append(PermissionReport(
                "System audio", PermissionStatus.DEVICE_UNAVAILABLE,
                f"{self.server} is running but reports no active output sink.",
                "Select an output device in your desktop's sound settings."))
        else:
            reports.append(PermissionReport(
                "System audio", PermissionStatus.SUPPORTED_AND_ALLOWED,
                f"{self.server} sink monitor: {sink.name}.monitor", ""))
        source = self.default_input()
        if source is None:
            reports.append(PermissionReport(
                "Microphone", PermissionStatus.DEVICE_UNAVAILABLE,
                f"{self.server} reports no active capture source.",
                "Connect a microphone or select one in your sound settings."))
        else:
            reports.append(PermissionReport(
                "Microphone", PermissionStatus.SUPPORTED_AND_ALLOWED,
                f"{self.server} source: {source.name}", ""))
        return reports

    def _unsupported_message(self) -> str:
        if self.server == "none":
            return ("No supported audio system detected: neither PipeWire nor "
                    "PulseAudio is available on this machine.")
        return (f"{self.server} is present but no capture helper "
                f"(parec or pw-record) was found on PATH.")

    def describe_system_capture(self) -> str:
        if not self.available:
            return "Linux (no supported audio server)"
        return f"Linux {self.server} sink monitor"

    def watch_hints(self) -> Dict[str, str]:
        """Extra context shown in diagnostics."""
        return {"server": self.server, "tool": self.tool}
