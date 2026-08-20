"""Internal audio format normalisation (spec 11).

Internal representation:

* 48,000 Hz
* float32
* stereo preserved for SYSTEM when the device provides it, mono for MIC
* mono derived on demand for transcription

Resampling keeps per-stream state so that consecutive blocks join without a
click at the seam.  It runs on the pump thread, never in a driver callback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

TARGET_SAMPLE_RATE = 48_000
DTYPE = np.float32


def to_float32(pcm: np.ndarray) -> np.ndarray:
    """Convert an int16/int32/float64 block to float32 in [-1, 1]."""
    if pcm.dtype == np.float32:
        return pcm
    if pcm.dtype == np.float64:
        return pcm.astype(np.float32)
    if pcm.dtype == np.int16:
        return (pcm.astype(np.float32) / 32768.0)
    if pcm.dtype == np.int32:
        return (pcm.astype(np.float32) / 2147483648.0)
    if pcm.dtype == np.uint8:
        return ((pcm.astype(np.float32) - 128.0) / 128.0)
    return pcm.astype(np.float32)


def deinterleave(raw: np.ndarray, channels: int) -> np.ndarray:
    """Reshape an interleaved 1-D buffer to ``(frames, channels)``."""
    if raw.ndim == 2:
        return raw
    channels = max(1, channels)
    frames = raw.shape[0] // channels
    return raw[: frames * channels].reshape(frames, channels)


def to_mono(pcm: np.ndarray) -> np.ndarray:
    """Average all channels down to a single one, shape ``(frames, 1)``."""
    if pcm.ndim == 1:
        return pcm.reshape(-1, 1).astype(DTYPE, copy=False)
    if pcm.shape[1] == 1:
        return pcm.astype(DTYPE, copy=False)
    return pcm.mean(axis=1, keepdims=True).astype(DTYPE, copy=False)


def dbfs(pcm: np.ndarray) -> float:
    """RMS level in dBFS.  Used for the meters and diagnostics (spec 14)."""
    if pcm.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(pcm.astype(np.float64)))))
    if rms <= 1e-9:
        return -120.0
    return max(-120.0, 20.0 * np.log10(rms))


def peak_dbfs(pcm: np.ndarray) -> float:
    if pcm.size == 0:
        return -120.0
    peak = float(np.max(np.abs(pcm)))
    if peak <= 1e-9:
        return -120.0
    return max(-120.0, 20.0 * np.log10(peak))


class Resampler:
    """Stateful linear-interpolation resampler.

    Linear interpolation is deliberate: it is cheap, allocation-light and
    good enough for speech heading into a speech-to-text model.  The state
    (the last input frame and the fractional read position) is what keeps
    block boundaries continuous.

    A changed input rate — which happens when a Bluetooth device switches
    profile mid-meeting — is handled by :meth:`reset`.
    """

    def __init__(self, src_rate: int, dst_rate: int = TARGET_SAMPLE_RATE, channels: int = 1) -> None:
        self.src_rate = int(src_rate)
        self.dst_rate = int(dst_rate)
        self.channels = int(channels)
        self._tail: Optional[np.ndarray] = None   # last input frame of the previous block
        self._pos = 0.0                            # fractional position into the current block

    def reset(self, src_rate: Optional[int] = None, channels: Optional[int] = None) -> None:
        if src_rate:
            self.src_rate = int(src_rate)
        if channels:
            self.channels = int(channels)
        self._tail = None
        self._pos = 0.0

    def process(self, pcm: np.ndarray) -> np.ndarray:
        """Resample one block, shape ``(frames, channels)`` in and out."""
        if pcm.ndim == 1:
            pcm = pcm.reshape(-1, 1)
        pcm = pcm.astype(DTYPE, copy=False)
        if self.src_rate == self.dst_rate:
            return pcm
        if pcm.shape[0] == 0:
            return pcm

        if self._tail is None or self._tail.shape[1] != pcm.shape[1]:
            self._tail = np.zeros((1, pcm.shape[1]), dtype=DTYPE)
            self._pos = 0.0

        # Prepend the previous block's final frame so interpolation can span
        # the seam; index 0 of `buf` is therefore "one frame before now".
        buf = np.concatenate([self._tail, pcm], axis=0)
        ratio = self.src_rate / float(self.dst_rate)

        # Output positions run from self._pos up to the last fully
        # interpolatable input frame.
        max_pos = buf.shape[0] - 1
        n_out = int(np.floor((max_pos - self._pos) / ratio))
        if n_out <= 0:
            self._tail = buf[-1:].copy()
            self._pos = max(0.0, self._pos - (buf.shape[0] - 1))
            return np.zeros((0, pcm.shape[1]), dtype=DTYPE)

        idx = self._pos + ratio * np.arange(n_out, dtype=np.float64)
        lo = np.floor(idx).astype(np.int64)
        frac = (idx - lo).astype(DTYPE).reshape(-1, 1)
        hi = np.minimum(lo + 1, max_pos)
        out = buf[lo] * (1.0 - frac) + buf[hi] * frac

        consumed = self._pos + ratio * n_out
        self._pos = consumed - (buf.shape[0] - 1)
        self._tail = buf[-1:].copy()
        return out.astype(DTYPE, copy=False)


@dataclass
class NormalizedBlock:
    """Result of normalising one chunk."""

    pcm: np.ndarray            # (frames, channels) float32 @ 48 kHz
    sample_rate: int
    channels: int
    timestamp_monotonic: int
    source: str
    sequence_number: int
    device: str = ""
    source_sample_rate: int = 0

    @property
    def frame_count(self) -> int:
        return int(self.pcm.shape[0])


class Normalizer:
    """Per-source normalisation to the internal format.

    ``max_channels`` caps the stored width: SYSTEM keeps stereo when the
    endpoint provides it, MIC collapses to mono.
    """

    def __init__(self, source: str, *, max_channels: int = 2) -> None:
        self.source = source
        self.max_channels = max(1, int(max_channels))
        self._resampler: Optional[Resampler] = None
        self._in_rate = 0
        self._in_channels = 0

    def process(self, chunk) -> NormalizedBlock:
        pcm = to_float32(deinterleave(chunk.pcm, chunk.channels))
        if pcm.shape[1] > self.max_channels:
            pcm = (to_mono(pcm) if self.max_channels == 1
                   else pcm[:, : self.max_channels])

        if (self._resampler is None
                or chunk.sample_rate != self._in_rate
                or pcm.shape[1] != self._in_channels):
            # A rate or width change means a new device or a new Bluetooth
            # profile; start the interpolator clean rather than smearing the
            # old state across the discontinuity.
            self._resampler = Resampler(chunk.sample_rate, TARGET_SAMPLE_RATE, pcm.shape[1])
            self._in_rate = chunk.sample_rate
            self._in_channels = pcm.shape[1]

        out = self._resampler.process(pcm)
        return NormalizedBlock(
            pcm=out,
            sample_rate=TARGET_SAMPLE_RATE,
            channels=int(out.shape[1]) if out.size else self._in_channels,
            timestamp_monotonic=chunk.timestamp_monotonic,
            source=chunk.source,
            sequence_number=chunk.sequence_number,
            device=chunk.device,
            source_sample_rate=chunk.sample_rate,
        )
