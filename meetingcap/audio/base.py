"""The ``AudioSource`` contract shared by every platform adapter.

Spec sections 4, 10, 12 and 15.

Two rules drive the design here:

* An audio callback may only *copy* frames into a bounded queue.  No
  transcription, networking, AI, compression or summarisation ever happens on
  the driver's thread.
* Every chunk carries enough metadata (monotonic timestamp, frame count,
  sample rate, sequence number) that a later stage can detect and correct
  clock drift without the capture path having to care.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass(frozen=True)
class AudioChunk:
    """One block of PCM as it left the driver.

    ``pcm`` is float32 with shape ``(frames, channels)``.  It is *not*
    resampled: adapters report whatever the device gave them and the
    normalisation stage (:mod:`meetingcap.audio.formats`) converts to the
    internal 48 kHz float32 format off the callback thread.
    """

    source: str                 # SYSTEM or MIC
    pcm: np.ndarray
    timestamp_monotonic: int    # time.monotonic_ns() at callback arrival
    frame_count: int
    sample_rate: int
    channels: int
    sequence_number: int
    device: str = ""

    @property
    def duration_s(self) -> float:
        return self.frame_count / float(self.sample_rate or 1)


@dataclass
class SourceHealth:
    """Snapshot consumed by the watchdog and the console UI (spec 15)."""

    source: str
    alive: bool = False
    device: str = ""
    sample_rate: int = 0
    channels: int = 0
    last_packet_ns: Optional[int] = None
    frames_captured: int = 0
    queue_overruns: int = 0
    restarts: int = 0
    error: Optional[str] = None

    def last_packet_age_ms(self, now_ns: Optional[int] = None) -> Optional[float]:
        if self.last_packet_ns is None:
            return None
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        return (now_ns - self.last_packet_ns) / 1e6


class AudioSourceError(RuntimeError):
    """Raised when a source cannot be opened or has been invalidated.

    ``recoverable`` tells the watchdog whether a restart is worth attempting;
    a missing OS permission, for instance, is not something a retry fixes.
    """

    def __init__(self, message: str, *, recoverable: bool = True) -> None:
        super().__init__(message)
        self.recoverable = recoverable


class AudioSource:
    """Base class for every capture adapter.

    Subclasses implement :meth:`_open` and :meth:`_close` and push chunks with
    :meth:`_emit`.  ``read``/``health`` and the bounded queue accounting are
    handled once, here, so that platform code stays small.
    """

    #: SYSTEM or MIC — set by the subclass.
    kind: str = ""

    def __init__(self, *, queue_frames: int = 256) -> None:
        # A bounded queue: under sustained overload we drop the *oldest*
        # audio and count it, rather than growing without limit or blocking
        # the driver thread.
        self._queue: "queue.Queue[AudioChunk]" = queue.Queue(maxsize=queue_frames)
        self._lock = threading.Lock()
        self._seq = 0
        self._health = SourceHealth(source=self.kind)
        self._running = False

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
        self._open()
        with self._lock:
            self._running = True
            self._health.alive = True
            self._health.error = None

    def stop(self) -> None:
        with self._lock:
            was_running = self._running
            self._running = False
            self._health.alive = False
        if was_running:
            try:
                self._close()
            except Exception as exc:  # pragma: no cover - defensive
                self._set_error(str(exc))

    def restart(self) -> None:
        """Close and reopen, keeping health counters and the queue intact."""
        self.stop()
        self.start()
        with self._lock:
            self._health.restarts += 1

    # -- data flow ------------------------------------------------------

    def read(self, timeout: float = 0.5) -> Optional[AudioChunk]:
        """Pop the next chunk, or ``None`` if none arrived within *timeout*."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def health(self) -> SourceHealth:
        with self._lock:
            # Copy so callers never observe a half-updated record.
            return SourceHealth(**vars(self._health))

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    # -- helpers for subclasses -----------------------------------------

    def _emit(self, pcm: np.ndarray, sample_rate: int, channels: int, device: str = "") -> None:
        """Queue a block of frames.  Safe to call from a driver callback.

        Does no allocation beyond the chunk itself and never blocks.
        """
        now = time.monotonic_ns()
        with self._lock:
            self._seq += 1
            seq = self._seq
        chunk = AudioChunk(
            source=self.kind,
            pcm=pcm,
            timestamp_monotonic=now,
            frame_count=int(pcm.shape[0]),
            sample_rate=sample_rate,
            channels=channels,
            sequence_number=seq,
            device=device,
        )
        try:
            self._queue.put_nowait(chunk)
        except queue.Full:
            # Drop the oldest chunk to keep latency bounded, then retry once.
            try:
                self._queue.get_nowait()
            except queue.Empty:  # pragma: no cover - race with the reader
                pass
            with self._lock:
                self._health.queue_overruns += 1
            try:
                self._queue.put_nowait(chunk)
            except queue.Full:  # pragma: no cover - reader is wedged
                return
        with self._lock:
            self._health.last_packet_ns = now
            self._health.frames_captured += chunk.frame_count
            self._health.device = device or self._health.device
            self._health.sample_rate = sample_rate
            self._health.channels = channels

    def _set_error(self, message: str) -> None:
        """Mark the stream as dead so the watchdog picks it up."""
        with self._lock:
            self._health.error = message
            self._health.alive = False
            self._running = False

    def _set_device(self, device: str, sample_rate: int = 0, channels: int = 0) -> None:
        with self._lock:
            self._health.device = device
            if sample_rate:
                self._health.sample_rate = sample_rate
            if channels:
                self._health.channels = channels

    # -- to implement ----------------------------------------------------

    def _open(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def _close(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def describe(self) -> str:
        h = self.health()
        return h.device or self.__class__.__name__


@dataclass
class DeviceInfo:
    """A device as reported by a platform adapter (spec 18 ``--list-devices``)."""

    index: str
    name: str
    kind: str                    # output / input / loopback
    sample_rate: int = 0
    channels: int = 0
    is_default: bool = False
    host_api: str = ""
    extra: dict = field(default_factory=dict)

    def line(self) -> str:
        star = "*" if self.is_default else " "
        rate = f"{self.sample_rate} Hz" if self.sample_rate else "-"
        ch = f"{self.channels}ch" if self.channels else "-"
        return f" {star} [{self.index}] {self.name}  ({self.kind}, {rate}, {ch})"
