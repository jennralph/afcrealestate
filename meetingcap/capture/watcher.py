"""Default-device watcher (spec 6).

A meeting may start on laptop speakers and continue on AirPods.  Roughly once
a second this thread asks the platform backend what the default render and
capture endpoints currently are and reports any change:

* default render endpoint changed
* default microphone changed
* device disappeared
* sample rate changed (a Bluetooth profile switch usually shows up this way)

The watcher only *detects*.  Reopening the affected stream — and keeping the
logical meeting alive while it happens — is the recorder's job.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from ..audio.base import DeviceInfo

SYSTEM_OUTPUT_CHANGED = "system_output_changed"
MICROPHONE_CHANGED = "microphone_changed"
SYSTEM_OUTPUT_LOST = "system_output_lost"
MICROPHONE_LOST = "microphone_lost"
SYSTEM_FORMAT_CHANGED = "system_format_changed"
MICROPHONE_FORMAT_CHANGED = "microphone_format_changed"


@dataclass
class DeviceChange:
    type: str
    source: str                 # SYSTEM or MIC
    from_device: str
    to_device: str
    from_sample_rate: int = 0
    to_sample_rate: int = 0

    @property
    def requires_reopen(self) -> bool:
        return self.type in (SYSTEM_OUTPUT_CHANGED, MICROPHONE_CHANGED,
                             SYSTEM_OUTPUT_LOST, MICROPHONE_LOST,
                             SYSTEM_FORMAT_CHANGED, MICROPHONE_FORMAT_CHANGED)


ChangeCallback = Callable[[DeviceChange], None]


def _identity(info: Optional[DeviceInfo]) -> str:
    return "" if info is None else f"{info.name}"


class DeviceWatcher(threading.Thread):
    """Polls the platform's default endpoints and reports transitions."""

    def __init__(self, backend, on_change: ChangeCallback, *,
                 interval: float = 1.0, watch_system: bool = True,
                 watch_microphone: bool = True) -> None:
        super().__init__(name="device-watcher", daemon=True)
        self.backend = backend
        self.on_change = on_change
        self.interval = interval
        self.watch_system = watch_system
        self.watch_microphone = watch_microphone
        self._stop_evt = threading.Event()
        self._output: Optional[DeviceInfo] = None
        self._input: Optional[DeviceInfo] = None
        self.poll_errors = 0

    def prime(self) -> None:
        """Record the current state without emitting change events."""
        self._output = self._safe(self.backend.default_output) if self.watch_system else None
        self._input = self._safe(self.backend.default_input) if self.watch_microphone else None

    def run(self) -> None:
        if self._output is None and self._input is None:
            self.prime()
        while not self._stop_evt.wait(self.interval):
            self.poll()

    def poll(self) -> None:
        """One comparison pass.  Exposed separately so tests can drive it."""
        if self.watch_system:
            self._compare(
                "SYSTEM", self._safe(self.backend.default_output), self._output,
                changed=SYSTEM_OUTPUT_CHANGED, lost=SYSTEM_OUTPUT_LOST,
                reformatted=SYSTEM_FORMAT_CHANGED)
        if self.watch_microphone:
            self._compare(
                "MIC", self._safe(self.backend.default_input), self._input,
                changed=MICROPHONE_CHANGED, lost=MICROPHONE_LOST,
                reformatted=MICROPHONE_FORMAT_CHANGED)

    def _compare(self, source: str, current: Optional[DeviceInfo],
                 previous: Optional[DeviceInfo], *, changed: str, lost: str,
                 reformatted: str) -> None:
        if source == "SYSTEM":
            self._output = current if current is not None else self._output
        else:
            self._input = current if current is not None else self._input

        if previous is None:
            return
        if current is None:
            self._emit(DeviceChange(lost, source, _identity(previous), "",
                                    previous.sample_rate, 0))
            return
        if _identity(current) != _identity(previous):
            self._emit(DeviceChange(changed, source, _identity(previous),
                                    _identity(current), previous.sample_rate,
                                    current.sample_rate))
        elif (current.sample_rate and previous.sample_rate
              and current.sample_rate != previous.sample_rate):
            self._emit(DeviceChange(reformatted, source, _identity(previous),
                                    _identity(current), previous.sample_rate,
                                    current.sample_rate))

    def _emit(self, change: DeviceChange) -> None:
        try:
            self.on_change(change)
        except Exception:  # pragma: no cover - a handler must not kill the watcher
            pass

    def _safe(self, fn) -> Optional[DeviceInfo]:
        try:
            return fn()
        except Exception:
            # Enumeration itself can fail while a device is being torn down.
            # That is not fatal; the next poll usually succeeds.
            self.poll_errors += 1
            return None

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_evt.set()
        if self.is_alive():
            self.join(timeout=timeout)


class Backoff:
    """Exponential backoff schedule from spec 15: 0.1, 0.25, 0.5, 1, 2, then 5s."""

    STEPS = (0.1, 0.25, 0.5, 1.0, 2.0)
    STEADY = 5.0

    def __init__(self) -> None:
        self._attempt = 0

    def next_delay(self) -> float:
        delay = self.STEPS[self._attempt] if self._attempt < len(self.STEPS) else self.STEADY
        self._attempt += 1
        return delay

    def reset(self) -> None:
        self._attempt = 0

    @property
    def attempts(self) -> int:
        return self._attempt

    def sleep(self) -> float:
        delay = self.next_delay()
        time.sleep(delay)
        return delay
