"""Clock-drift monitoring (spec 12).

SYSTEM and MICROPHONE are two independent hardware clocks.  Equal frame
counts do not stay synchronised over a two-hour meeting, so every source is
measured against ``time.monotonic_ns()`` and the divergence is reported.

Nothing is corrected here: per spec, correction belongs to the later
resampling/mixing stage, not to the capture path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class DriftReport:
    source: str
    frames: int = 0
    audio_seconds: float = 0.0
    wall_seconds: float = 0.0
    drift_seconds: float = 0.0
    drift_ppm: float = 0.0
    samples: int = 0

    def as_dict(self) -> Dict[str, float]:
        return {
            "frames": self.frames,
            "audio_seconds": round(self.audio_seconds, 3),
            "wall_seconds": round(self.wall_seconds, 3),
            "drift_seconds": round(self.drift_seconds, 4),
            "drift_ppm": round(self.drift_ppm, 1),
        }


class DriftMonitor:
    """Tracks how far one source's sample clock has run from the wall clock."""

    def __init__(self, source: str, sample_rate: int = 48_000) -> None:
        self.source = source
        self.sample_rate = sample_rate
        self._first_ns: Optional[int] = None
        self._last_ns: Optional[int] = None
        self._frames = 0
        self._samples = 0

    def observe(self, frames: int, timestamp_monotonic: int) -> None:
        if frames <= 0:
            return
        block_start = timestamp_monotonic - int(frames / float(self.sample_rate) * 1e9)
        if self._first_ns is None:
            self._first_ns = block_start
        self._last_ns = timestamp_monotonic
        self._frames += frames
        self._samples += 1

    def report(self) -> DriftReport:
        report = DriftReport(source=self.source, frames=self._frames, samples=self._samples)
        if self._first_ns is None or self._last_ns is None:
            return report
        report.audio_seconds = self._frames / float(self.sample_rate)
        report.wall_seconds = (self._last_ns - self._first_ns) / 1e9
        report.drift_seconds = report.audio_seconds - report.wall_seconds
        if report.wall_seconds > 0:
            report.drift_ppm = (report.drift_seconds / report.wall_seconds) * 1e6
        return report

    @property
    def first_timestamp_ns(self) -> Optional[int]:
        return self._first_ns


@dataclass
class SyncState:
    """Cross-source view used by diagnostics and the mixer."""

    monitors: Dict[str, DriftMonitor] = field(default_factory=dict)

    def monitor(self, source: str, sample_rate: int = 48_000) -> DriftMonitor:
        if source not in self.monitors:
            self.monitors[source] = DriftMonitor(source, sample_rate)
        return self.monitors[source]

    def observe(self, source: str, frames: int, timestamp_monotonic: int,
                sample_rate: int = 48_000) -> None:
        self.monitor(source, sample_rate).observe(frames, timestamp_monotonic)

    def reports(self) -> Dict[str, Dict[str, float]]:
        return {name: mon.report().as_dict() for name, mon in self.monitors.items()}

    def relative_offset_seconds(self, a: str, b: str) -> Optional[float]:
        """Start-time offset between two sources, positive when *a* began later."""
        ma, mb = self.monitors.get(a), self.monitors.get(b)
        if not ma or not mb or ma.first_timestamp_ns is None or mb.first_timestamp_ns is None:
            return None
        return (ma.first_timestamp_ns - mb.first_timestamp_ns) / 1e9


def monotonic_to_session_seconds(timestamp_ns: int, session_start_ns: int) -> float:
    """Convert a chunk timestamp to seconds since the session began."""
    return (timestamp_ns - session_start_ns) / 1e9


def now_ns() -> int:
    return time.monotonic_ns()
