"""Watchdog and automatic stream recovery (spec 15).

Every second, for each track:

* is the stream alive?
* how old is the latest packet?
* is the queue backing up?
* is the disk writer alive?
* is there enough disk space?

A stream that stops unexpectedly is restarted *without terminating the
recording*, on the 0.1 / 0.25 / 0.5 / 1 / 2 s backoff schedule and then at a
steady interval.  The session stays open across the interruption; the gap is
timestamped in ``session.json`` and padded with silence on disk so the two
tracks stay aligned.
"""

from __future__ import annotations

import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from ..audio.base import AudioSource
from .watcher import Backoff
from .writer import TrackWriter

DEFAULT_STALL_TIMEOUT_S = 5.0
DISK_WARN_BYTES = 500 << 20      # 500 MiB
DISK_CRITICAL_BYTES = 100 << 20  # 100 MiB


@dataclass
class TrackTarget:
    """One watched track."""

    source: AudioSource
    writer: TrackWriter
    backoff: Backoff = field(default_factory=Backoff)
    next_attempt_monotonic: float = 0.0
    outage_started: Optional[float] = None
    restart_failures: int = 0
    restarts: int = 0
    last_error: Optional[str] = None
    #: True once the source reported a non-recoverable condition (a missing
    #: OS permission, say).  Retrying that is pointless and noisy.
    permanently_failed: bool = False
    #: Set while the recorder is deliberately reopening this track after a
    #: device change, so the two recovery paths never fight each other.
    suspended: bool = False


RestartHook = Callable[[str, str], None]   # (source, reason)


class Watchdog(threading.Thread):
    """Supervises the capture streams and the disk writers."""

    def __init__(self, output_dir: str, *, interval: float = 1.0,
                 stall_timeout: float = DEFAULT_STALL_TIMEOUT_S,
                 on_outage: Optional[RestartHook] = None,
                 on_recovery: Optional[RestartHook] = None) -> None:
        super().__init__(name="watchdog", daemon=True)
        self.output_dir = output_dir
        self.interval = interval
        self.stall_timeout = stall_timeout
        self.on_outage = on_outage
        self.on_recovery = on_recovery
        self.targets: Dict[str, TrackTarget] = {}
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self.disk_free_bytes = 0
        self.disk_warning: Optional[str] = None

    # -- registration ----------------------------------------------------

    def watch(self, name: str, source: AudioSource, writer: TrackWriter) -> TrackTarget:
        target = TrackTarget(source=source, writer=writer)
        with self._lock:
            self.targets[name] = target
        return target

    def replace_source(self, name: str, source: AudioSource) -> None:
        """Swap in a freshly opened source after a device change."""
        with self._lock:
            target = self.targets.get(name)
            if target is not None:
                target.source = source
                target.backoff.reset()
                target.next_attempt_monotonic = 0.0
                target.outage_started = None
                target.permanently_failed = False

    def suspend(self, name: str) -> None:
        """Stop supervising a track while the recorder reopens it."""
        with self._lock:
            target = self.targets.get(name)
            if target is not None:
                target.suspended = True

    def resume(self, name: str) -> None:
        with self._lock:
            target = self.targets.get(name)
            if target is not None:
                target.suspended = False
                target.backoff.reset()
                target.next_attempt_monotonic = 0.0
                target.outage_started = None

    # -- thread ----------------------------------------------------------

    def run(self) -> None:
        while not self._stop_evt.wait(self.interval):
            try:
                self.check()
            except Exception:  # pragma: no cover - the watchdog never dies
                continue

    def check(self) -> None:
        """One supervision pass.  Exposed separately so tests can drive it."""
        self._check_disk()
        now = time.monotonic()
        with self._lock:
            targets = list(self.targets.items())
        for name, target in targets:
            self._check_target(name, target, now)

    def _check_target(self, name: str, target: TrackTarget, now: float) -> None:
        if target.permanently_failed or target.suspended:
            return
        health = target.source.health()
        stalled = False
        if health.alive:
            age_ms = health.last_packet_age_ms()
            stalled = age_ms is not None and age_ms > self.stall_timeout * 1000.0

        if health.alive and not stalled:
            if target.outage_started is not None:
                duration = now - target.outage_started
                target.outage_started = None
                target.backoff.reset()
                target.restart_failures = 0
                if self.on_recovery:
                    self.on_recovery(name, f"recovered after {duration:.2f}s")
            return

        # The stream is down or has gone quiet for longer than a driver ever
        # legitimately would.  Restart it, keeping the session alive.
        if target.outage_started is None:
            target.outage_started = now
            reason = health.error or ("stalled: no packets for "
                                      f"{self.stall_timeout:.1f}s")
            target.last_error = reason
            if self.on_outage:
                self.on_outage(name, reason)
        if now < target.next_attempt_monotonic:
            return
        delay = target.backoff.next_delay()
        target.next_attempt_monotonic = now + delay
        try:
            target.source.stop()
            target.source.start()
            target.restarts += 1
        except Exception as exc:
            target.restart_failures += 1
            target.last_error = str(exc)
            if getattr(exc, "recoverable", True) is False:
                target.permanently_failed = True

    def _check_disk(self) -> None:
        try:
            usage = shutil.disk_usage(self.output_dir)
        except OSError:  # pragma: no cover - path vanished
            return
        self.disk_free_bytes = usage.free
        if usage.free < DISK_CRITICAL_BYTES:
            self.disk_warning = (
                f"CRITICAL: {usage.free / (1 << 20):.0f} MB free on the recording volume")
        elif usage.free < DISK_WARN_BYTES:
            self.disk_warning = (
                f"low disk space: {usage.free / (1 << 20):.0f} MB free")
        else:
            self.disk_warning = None

    # -- reporting -------------------------------------------------------

    def health(self) -> Dict[str, object]:
        """The health dictionary described in spec 15."""
        report: Dict[str, object] = {}
        with self._lock:
            targets = list(self.targets.items())
        for name, target in targets:
            h = target.source.health()
            stats = target.writer.stats()
            key = "system_audio" if name == "SYSTEM" else "microphone"
            prefix = "system" if name == "SYSTEM" else "mic"
            age = h.last_packet_age_ms()
            report[key] = bool(h.alive)
            report[f"{prefix}_last_packet_ms"] = None if age is None else round(age)
            report[f"{prefix}_device"] = h.device
            report[f"{prefix}_restarts"] = target.restarts
            report[f"{prefix}_writer_alive"] = stats.alive
            report[f"{prefix}_queue_depth"] = stats.queue_depth
            report.setdefault("queue_overruns", 0)
            report["queue_overruns"] = int(report["queue_overruns"]) + \
                h.queue_overruns + stats.queue_overruns
        report["disk_free_mb"] = round(self.disk_free_bytes / (1 << 20))
        report["disk_warning"] = self.disk_warning
        return report

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_evt.set()
        if self.is_alive():
            self.join(timeout=timeout)
