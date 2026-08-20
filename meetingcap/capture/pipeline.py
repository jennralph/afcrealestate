"""The pump that connects a source to the disk writer (spec 10, 14, 22).

    SYSTEM CALLBACK ---> bounded queue ---> normalise ---> system writer
    MIC CALLBACK    ---> bounded queue ---> normalise ---> microphone writer
                                              |
                                              +--> level meters, drift monitor
                                              +--> on_audio_chunk subscribers

Everything expensive happens on this thread, never on the driver's.  The
``on_audio_chunk`` subscriber list is the boundary a later transcription
phase plugs into; the recorder itself has no opinion about what consumes it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

import numpy as np

from ..audio.base import AudioSource
from ..audio.formats import Normalizer, dbfs, peak_dbfs, to_mono
from .sync import SyncState
from .writer import TrackWriter

#: ``callback(source, pcm, timestamp_monotonic_ns)`` — see spec 22.
ChunkSubscriber = Callable[[str, np.ndarray, int], None]


@dataclass
class LevelMeter:
    """Smoothed RMS/peak levels for the console meters (spec 14).

    Silence is a diagnostic signal only.  It never stops the recording.
    """

    rms_db: float = -120.0
    peak_db: float = -120.0
    updated_ns: int = 0
    smoothing: float = 0.6

    def update(self, pcm: np.ndarray) -> None:
        if pcm.size == 0:
            return
        rms = dbfs(pcm)
        peak = peak_dbfs(pcm)
        self.rms_db = self.smoothing * self.rms_db + (1 - self.smoothing) * rms
        self.peak_db = max(peak, self.peak_db - 3.0)
        self.updated_ns = time.monotonic_ns()

    def bar(self, width: int = 10, floor_db: float = -60.0) -> str:
        """Render ``#######...`` style meter used by the UI (spec 19)."""
        level = max(floor_db, min(0.0, self.rms_db))
        filled = int(round((level - floor_db) / (0.0 - floor_db) * width))
        filled = max(0, min(width, filled))
        return "#" * filled + "." * (width - filled)


class TrackPipeline(threading.Thread):
    """Moves one source's audio from the driver queue to disk."""

    def __init__(self, source: AudioSource, writer: TrackWriter, *,
                 sync: Optional[SyncState] = None, max_channels: int = 2,
                 subscribers: Optional[List[ChunkSubscriber]] = None) -> None:
        super().__init__(name=f"pump-{source.kind.lower()}", daemon=True)
        self.source = source
        self.writer = writer
        self.sync = sync or SyncState()
        self.normalizer = Normalizer(source.kind, max_channels=max_channels)
        self.meter = LevelMeter()
        self.subscribers: List[ChunkSubscriber] = list(subscribers or [])
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self.blocks_processed = 0
        self.current_device = ""
        self.subscriber_errors = 0

    def subscribe(self, callback: ChunkSubscriber) -> None:
        with self._lock:
            self.subscribers.append(callback)

    def run(self) -> None:
        while not self._stop_evt.is_set():
            chunk = self.source.read(timeout=0.25)
            if chunk is None:
                continue
            try:
                self._process(chunk)
            except Exception:  # pragma: no cover - never kill the pump
                continue

    def _process(self, chunk) -> None:
        block = self.normalizer.process(chunk)
        if block.pcm.size == 0:
            return
        self.writer.submit(block)
        self.sync.observe(block.source, block.frame_count,
                          block.timestamp_monotonic, block.sample_rate)
        self.meter.update(block.pcm)
        self.blocks_processed += 1
        if chunk.device:
            self.current_device = chunk.device

        if self.subscribers:
            # Transcription wants mono; hand out a copy so a subscriber can
            # never mutate what went to disk.
            mono = to_mono(block.pcm).reshape(-1).copy()
            with self._lock:
                subscribers = list(self.subscribers)
            for callback in subscribers:
                try:
                    callback(block.source, mono, block.timestamp_monotonic)
                except Exception:
                    self.subscriber_errors += 1

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_evt.set()
        if self.is_alive():
            self.join(timeout=timeout)
