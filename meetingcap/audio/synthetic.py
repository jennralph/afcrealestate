"""A synthetic backend: no drivers, no permissions, no hardware.

It exists for three reasons:

* the automated tests need to exercise the whole pipeline on a machine with
  no sound card (CI containers included);
* ``--diagnostics`` can verify timestamping, resampling and the disk writer
  even where system capture is unavailable;
* it makes failure modes reproducible — device switches, stalls and
  invalidated streams can be triggered on demand instead of by unplugging a
  headset at the right moment.

It is never selected automatically: the user has to ask for it with
``--source synthetic``.
"""

from __future__ import annotations

import math
import threading
import time
from typing import List, Optional

import numpy as np

from ..permissions import PermissionReport, PermissionStatus
from .backend import PlatformBackend
from .base import AudioSource, AudioSourceError, DeviceInfo


def _named(device: Optional[str]) -> Optional[str]:
    """``"auto"`` means "whatever the backend's default is", not a name."""
    return None if device in (None, "", "auto") else device


class SyntheticSource(AudioSource):
    """Generates a tone (or silence) in real time on its own thread."""

    def __init__(self, kind: str, *, sample_rate: int = 48_000, channels: int = 1,
                 block_frames: int = 480, frequency: float = 440.0,
                 amplitude: float = 0.25, device: str = "Synthetic Device",
                 realtime: bool = True, fail_after_s: Optional[float] = None,
                 stall_after_s: Optional[float] = None) -> None:
        self.kind = kind
        super().__init__()
        self.sample_rate = sample_rate
        self.channels = channels
        self.block_frames = block_frames
        self.frequency = frequency
        self.amplitude = amplitude
        self.device = device
        self.realtime = realtime
        self.fail_after_s = fail_after_s
        self.stall_after_s = stall_after_s
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._phase = 0.0
        self._set_device(device, sample_rate, channels)

    def _open(self) -> None:
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._generate, name=f"synthetic-{self.kind}",
                                        daemon=True)
        self._thread.start()

    def _close(self) -> None:
        self._stop_evt.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _generate(self) -> None:
        started = time.monotonic()
        block_seconds = self.block_frames / float(self.sample_rate)
        next_deadline = started
        step = 2 * math.pi * self.frequency / self.sample_rate
        while not self._stop_evt.is_set():
            elapsed = time.monotonic() - started
            if self.fail_after_s is not None and elapsed >= self.fail_after_s:
                self._set_error("synthetic stream invalidated")
                return
            if self.stall_after_s is not None and elapsed >= self.stall_after_s:
                # Alive but silent: exactly what a wedged driver looks like.
                self._stop_evt.wait(0.1)
                continue

            idx = np.arange(self.block_frames, dtype=np.float32)
            wave = (self.amplitude * np.sin(self._phase + step * idx)).astype(np.float32)
            self._phase = (self._phase + step * self.block_frames) % (2 * math.pi)
            pcm = np.repeat(wave.reshape(-1, 1), self.channels, axis=1)
            self._emit(pcm, self.sample_rate, self.channels, self.device)

            next_deadline += block_seconds
            if self.realtime:
                sleep_for = next_deadline - time.monotonic()
                if sleep_for > 0:
                    self._stop_evt.wait(sleep_for)
                else:
                    next_deadline = time.monotonic()
            else:
                self._stop_evt.wait(0.001)

    def set_device(self, device: str, sample_rate: Optional[int] = None) -> None:
        """Simulate the endpoint moving to different hardware."""
        self.device = device
        if sample_rate:
            self.sample_rate = sample_rate
        self._set_device(device, self.sample_rate, self.channels)


class SyntheticBackend(PlatformBackend):
    """Backend that hands out :class:`SyntheticSource` instances."""

    name = "Synthetic"

    def __init__(self, *, output_device: str = "Synthetic Speakers",
                 input_device: str = "Synthetic Microphone",
                 sample_rate: int = 48_000, system_channels: int = 2,
                 available: bool = True) -> None:
        self.output_device = output_device
        self.input_device = input_device
        self.sample_rate = sample_rate
        self.system_channels = system_channels
        self.available = available
        self.created: List[SyntheticSource] = []

    # -- enumeration -----------------------------------------------------

    def list_devices(self) -> List[DeviceInfo]:
        return [
            DeviceInfo("0", self.output_device, "output", self.sample_rate,
                       self.system_channels, is_default=True, host_api="Synthetic"),
            DeviceInfo("0-loopback", f"{self.output_device} [loopback]", "loopback",
                       self.sample_rate, self.system_channels, is_default=True,
                       host_api="Synthetic"),
            DeviceInfo("1", self.input_device, "input", self.sample_rate, 1,
                       is_default=True, host_api="Synthetic"),
        ]

    def default_output(self) -> Optional[DeviceInfo]:
        if not self.available:
            return None
        return DeviceInfo("0", self.output_device, "output", self.sample_rate,
                          self.system_channels, is_default=True)

    def default_input(self) -> Optional[DeviceInfo]:
        if not self.available:
            return None
        return DeviceInfo("1", self.input_device, "input", self.sample_rate, 1,
                          is_default=True)

    # -- sources ---------------------------------------------------------

    def create_system_source(self, device: Optional[str] = None) -> AudioSource:
        if not self.available:
            raise AudioSourceError("synthetic backend disabled", recoverable=False)
        source = SyntheticSource("SYSTEM", sample_rate=self.sample_rate,
                                 channels=self.system_channels, frequency=440.0,
                                 device=_named(device) or self.output_device)
        self.created.append(source)
        return source

    def create_microphone_source(self, device: Optional[str] = None) -> AudioSource:
        if not self.available:
            raise AudioSourceError("synthetic backend disabled", recoverable=False)
        source = SyntheticSource("MIC", sample_rate=self.sample_rate, channels=1,
                                 frequency=220.0, amplitude=0.15,
                                 device=_named(device) or self.input_device)
        self.created.append(source)
        return source

    # -- permissions -----------------------------------------------------

    def check_permissions(self) -> List[PermissionReport]:
        status = (PermissionStatus.SUPPORTED_AND_ALLOWED if self.available
                  else PermissionStatus.DEVICE_UNAVAILABLE)
        return [
            PermissionReport("System audio", status, "synthetic source", ""),
            PermissionReport("Microphone", status, "synthetic source", ""),
        ]

    def describe_system_capture(self) -> str:
        return "Synthetic generator (no hardware)"
