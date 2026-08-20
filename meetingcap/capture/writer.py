"""Crash-resistant segmented disk writer (spec 10, 11, 16).

One writer thread per track pulls from a bounded queue and appends to a WAV
segment that rotates roughly every five minutes.  Nothing is held only in
RAM: if the process dies late in a four-hour meeting, every closed segment is
already complete and the open one is recoverable via
:func:`meetingcap.capture.wavio.repair_wav`.

The writer also keeps the two tracks on a common wall-clock timeline.  When a
device switch interrupts a stream, the resulting hole is filled with silence
so that ``system.wav`` and ``microphone.wav`` stay aligned with each other —
which is what makes later transcript timestamps mean the same thing on both
tracks.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from .session import SessionMetadata
from .wavio import WavWriter, read_wav

DEFAULT_ROTATE_SECONDS = 300.0          # ~5 minutes (spec 16)
DEFAULT_MAX_SEGMENT_BYTES = 256 << 20   # 256 MiB safety net
MAX_GAP_PAD_SECONDS = 120.0             # never invent more silence than this
SYNC_INTERVAL_SECONDS = 2.0


@dataclass
class WriterStats:
    frames_written: int = 0
    segments: int = 0
    queue_depth: int = 0
    queue_overruns: int = 0
    silence_padded_s: float = 0.0
    last_write_ns: Optional[int] = None
    alive: bool = False
    error: Optional[str] = None


class TrackWriter(threading.Thread):
    """Writes one normalised track to rotating WAV segments."""

    def __init__(self, source: str, directory: str, session: SessionMetadata, *,
                 sample_rate: int = 48_000, channels: int = 1,
                 fmt: str = "int16", rotate_seconds: float = DEFAULT_ROTATE_SECONDS,
                 queue_blocks: int = 512) -> None:
        super().__init__(name=f"writer-{source.lower()}", daemon=True)
        self.source = source
        self.directory = directory
        self.session = session
        self.sample_rate = sample_rate
        self.channels = channels
        self.fmt = fmt
        self.rotate_seconds = rotate_seconds
        self.prefix = "system" if source == "SYSTEM" else "mic"

        self._queue: "queue.Queue[Optional[object]]" = queue.Queue(maxsize=queue_blocks)
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._stats = WriterStats()
        self._writer: Optional[WavWriter] = None
        self._segment_index = 0
        self._segment_files: List[str] = []
        self._segment_start_ns: Optional[int] = None
        self._expected_ns: Optional[int] = None
        self._last_sync = 0.0
        os.makedirs(directory, exist_ok=True)

    # -- producer side ---------------------------------------------------

    def submit(self, block) -> bool:
        """Hand a normalised block to the writer.  Never blocks.

        Returns ``False`` when the queue was full and the oldest block had to
        be dropped — counted as an overrun so the watchdog can surface it.
        """
        try:
            self._queue.put_nowait(block)
            return True
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:  # pragma: no cover - race with the writer
                pass
            with self._lock:
                self._stats.queue_overruns += 1
            try:
                self._queue.put_nowait(block)
            except queue.Full:  # pragma: no cover
                pass
            return False

    def stats(self) -> WriterStats:
        with self._lock:
            snapshot = WriterStats(**vars(self._stats))
        snapshot.queue_depth = self._queue.qsize()
        snapshot.alive = self.is_alive() and not self._stop_evt.is_set()
        return snapshot

    @property
    def segment_files(self) -> List[str]:
        with self._lock:
            return list(self._segment_files)

    # -- thread ----------------------------------------------------------

    def run(self) -> None:
        with self._lock:
            self._stats.alive = True
        try:
            while not self._stop_evt.is_set():
                try:
                    block = self._queue.get(timeout=0.25)
                except queue.Empty:
                    self._maybe_sync()
                    continue
                if block is None:
                    break
                self._write_block(block)
                self._maybe_sync()
            # Drain whatever is still queued so a clean stop loses nothing.
            while True:
                try:
                    block = self._queue.get_nowait()
                except queue.Empty:
                    break
                if block is not None:
                    self._write_block(block)
        except Exception as exc:  # pragma: no cover - defensive
            with self._lock:
                self._stats.error = str(exc)
        finally:
            self._close_segment()
            with self._lock:
                self._stats.alive = False

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_evt.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:  # pragma: no cover
            pass
        if self.is_alive():
            self.join(timeout=timeout)

    # -- writing ---------------------------------------------------------

    def _write_block(self, block) -> None:
        pcm: np.ndarray = block.pcm
        if pcm.size == 0:
            return
        channels = int(pcm.shape[1])
        start_ns = block.timestamp_monotonic - int(
            pcm.shape[0] / float(self.sample_rate) * 1e9)

        if self._writer is None or channels != self.channels:
            if self._writer is not None and channels != self.channels:
                # Channel width changed under us (new endpoint); a WAV file
                # cannot change width mid-stream, so start a fresh segment.
                self._close_segment()
            self.channels = channels
            self._open_segment(start_ns)
        elif (self._writer.duration_s >= self.rotate_seconds
              or self._writer.frames_written * channels * 4 >= DEFAULT_MAX_SEGMENT_BYTES):
            self._close_segment()
            self._open_segment(start_ns)
        else:
            self._pad_gap(start_ns)

        assert self._writer is not None
        self._writer.write(pcm)
        with self._lock:
            self._stats.frames_written += int(pcm.shape[0])
            self._stats.last_write_ns = time.monotonic_ns()
        self._expected_ns = start_ns + int(pcm.shape[0] / float(self.sample_rate) * 1e9)

    def _pad_gap(self, start_ns: int) -> None:
        """Insert silence for time the device was not producing audio."""
        if self._expected_ns is None or self._writer is None:
            return
        gap_s = (start_ns - self._expected_ns) / 1e9
        if gap_s <= 0.05:                     # jitter, not a real hole
            return
        gap_s = min(gap_s, MAX_GAP_PAD_SECONDS)
        frames = int(gap_s * self.sample_rate)
        if frames <= 0:
            return
        self._writer.write(np.zeros((frames, self.channels), dtype=np.float32))
        with self._lock:
            self._stats.frames_written += frames
            self._stats.silence_padded_s += gap_s

    def _open_segment(self, start_ns: int) -> None:
        self._segment_index += 1
        filename = f"{self.prefix}_{self._segment_index:04d}.wav"
        path = os.path.join(self.directory, filename)
        self._writer = WavWriter(path, self.sample_rate, self.channels, fmt=self.fmt)
        self._segment_start_ns = start_ns
        self._expected_ns = start_ns
        with self._lock:
            self._segment_files.append(path)
            self._stats.segments += 1
        self.session.add_segment(self.source, os.path.join(
            os.path.basename(self.directory), filename),
            start_ns, self.sample_rate, self.channels)

    def _close_segment(self) -> None:
        if self._writer is None:
            return
        writer, self._writer = self._writer, None
        frames = writer.frames_written
        writer.close()
        self.session.close_segment(os.path.join(
            os.path.basename(self.directory), os.path.basename(writer.path)), frames)

    def _maybe_sync(self) -> None:
        now = time.monotonic()
        if self._writer is not None and now - self._last_sync >= SYNC_INTERVAL_SECONDS:
            self._writer.sync()
            self._last_sync = now


def concat_segments(segment_paths: List[str], out_path: str, fmt: str = "int16") -> Optional[str]:
    """Join a track's segments into a single continuous WAV.

    Used at the end of a session to produce ``system.wav`` /
    ``microphone.wav`` while leaving the crash-safe segments in place.
    """
    usable = [p for p in segment_paths if os.path.exists(p) and os.path.getsize(p) > 44]
    if not usable:
        return None
    writer: Optional[WavWriter] = None
    try:
        for path in usable:
            pcm, rate = read_wav(path)
            if pcm.size == 0:
                continue
            if writer is None:
                writer = WavWriter(out_path, rate, int(pcm.shape[1]), fmt=fmt)
            writer.write(pcm)
    finally:
        if writer is not None:
            writer.close()
    return out_path if writer is not None else None
