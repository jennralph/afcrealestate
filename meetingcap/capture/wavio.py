"""Minimal RIFF/WAVE reader and writer (stdlib only).

The stdlib :mod:`wave` module cannot write IEEE float32, and — more
importantly for spec 16 — it gives no way to repair a file whose header was
never finalised because the process died mid-meeting.  Both matter here, so
the few dozen lines of RIFF are written out by hand.

Every segment is opened with a placeholder size in its header and the header
is patched on close.  :func:`repair_wav` recomputes those sizes from the
file length, which is what makes an abruptly-killed recording readable.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import BinaryIO, Optional, Tuple

import numpy as np

WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3


@dataclass
class WavInfo:
    sample_rate: int
    channels: int
    bits_per_sample: int
    audio_format: int
    data_offset: int
    data_size: int

    @property
    def frame_count(self) -> int:
        bytes_per_frame = self.channels * self.bits_per_sample // 8
        return self.data_size // max(1, bytes_per_frame)

    @property
    def duration_s(self) -> float:
        return self.frame_count / float(self.sample_rate or 1)


class WavWriter:
    """Streaming WAV writer for float32 blocks.

    ``fmt`` is ``"int16"`` (default, maximum compatibility) or ``"float32"``
    (bit-exact with the internal representation).
    """

    def __init__(self, path: str, sample_rate: int, channels: int, fmt: str = "int16") -> None:
        if fmt not in ("int16", "float32"):
            raise ValueError(f"unsupported wav format: {fmt}")
        self.path = path
        self.sample_rate = int(sample_rate)
        self.channels = max(1, int(channels))
        self.fmt = fmt
        self.frames_written = 0
        self._closed = False
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._fh: BinaryIO = open(path, "wb")
        self._data_offset = self._write_header()

    # -- header ---------------------------------------------------------

    @property
    def bits_per_sample(self) -> int:
        return 16 if self.fmt == "int16" else 32

    @property
    def audio_format(self) -> int:
        return WAVE_FORMAT_PCM if self.fmt == "int16" else WAVE_FORMAT_IEEE_FLOAT

    def _write_header(self) -> int:
        block_align = self.channels * self.bits_per_sample // 8
        byte_rate = self.sample_rate * block_align
        fh = self._fh
        fh.write(b"RIFF")
        fh.write(struct.pack("<I", 0))          # patched on close
        fh.write(b"WAVE")
        if self.audio_format == WAVE_FORMAT_PCM:
            fh.write(b"fmt ")
            fh.write(struct.pack("<IHHIIHH", 16, WAVE_FORMAT_PCM, self.channels,
                                 self.sample_rate, byte_rate, block_align,
                                 self.bits_per_sample))
        else:
            fh.write(b"fmt ")
            fh.write(struct.pack("<IHHIIHHH", 18, WAVE_FORMAT_IEEE_FLOAT, self.channels,
                                 self.sample_rate, byte_rate, block_align,
                                 self.bits_per_sample, 0))
            fh.write(b"fact")
            fh.write(struct.pack("<II", 4, 0))  # sample count, patched on close
        fh.write(b"data")
        fh.write(struct.pack("<I", 0))          # patched on close
        fh.flush()
        return fh.tell()

    # -- data -----------------------------------------------------------

    def write(self, pcm: np.ndarray) -> int:
        """Append float32 frames, shape ``(frames, channels)`` or ``(frames,)``."""
        if self._closed:
            raise ValueError("writer is closed")
        if pcm.size == 0:
            return 0
        if pcm.ndim == 1:
            pcm = pcm.reshape(-1, 1)
        if pcm.shape[1] != self.channels:
            if pcm.shape[1] == 1:
                pcm = np.repeat(pcm, self.channels, axis=1)
            else:
                pcm = pcm[:, : self.channels]
        if self.fmt == "int16":
            clipped = np.clip(pcm, -1.0, 1.0)
            data = (clipped * 32767.0).astype("<i2")
        else:
            data = pcm.astype("<f4", copy=False)
        self._fh.write(data.tobytes())
        self.frames_written += int(pcm.shape[0])
        return int(pcm.shape[0])

    def flush(self) -> None:
        if not self._closed:
            self._fh.flush()

    def sync(self) -> None:
        """Force the OS to persist what has been written so far."""
        if self._closed:
            return
        self._fh.flush()
        try:
            os.fsync(self._fh.fileno())
        except OSError:  # pragma: no cover - platform dependent
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            data_size = self.frames_written * self.channels * self.bits_per_sample // 8
            self._fh.flush()
            self._fh.seek(4)
            self._fh.write(struct.pack("<I", self._data_offset + data_size - 8))
            if self.audio_format == WAVE_FORMAT_IEEE_FLOAT:
                self._fh.seek(self._data_offset - 12 - 8)  # fact chunk payload
                self._fh.write(struct.pack("<I", self.frames_written))
            self._fh.seek(self._data_offset - 4)
            self._fh.write(struct.pack("<I", data_size))
            self._fh.flush()
        finally:
            self._fh.close()

    def __enter__(self) -> "WavWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def duration_s(self) -> float:
        return self.frames_written / float(self.sample_rate or 1)


# -- reading / repair ---------------------------------------------------


def parse_wav_header(path: str) -> Optional[WavInfo]:
    """Read the format and locate the data chunk.  ``None`` if unparsable."""
    with open(path, "rb") as fh:
        if fh.read(4) != b"RIFF":
            return None
        fh.read(4)
        if fh.read(4) != b"WAVE":
            return None
        audio_format = channels = sample_rate = bits = 0
        while True:
            head = fh.read(8)
            if len(head) < 8:
                return None
            cid, size = struct.unpack("<4sI", head)
            if cid == b"fmt ":
                body = fh.read(size)
                audio_format, channels, sample_rate, _, _, bits = struct.unpack(
                    "<HHIIHH", body[:16])
            elif cid == b"data":
                return WavInfo(sample_rate=sample_rate, channels=channels,
                               bits_per_sample=bits, audio_format=audio_format,
                               data_offset=fh.tell(), data_size=size)
            else:
                fh.seek(size + (size & 1), os.SEEK_CUR)


def repair_wav(path: str) -> Optional[WavInfo]:
    """Recompute the RIFF/data sizes of a segment left open by a crash.

    Returns the repaired :class:`WavInfo`, or ``None`` if the file has no
    usable header.  Safe to run on an already-correct file.
    """
    info = parse_wav_header(path)
    if info is None:
        return None
    file_size = os.path.getsize(path)
    available = file_size - info.data_offset
    if available < 0:
        return None
    bytes_per_frame = max(1, info.channels * info.bits_per_sample // 8)
    # Truncate a torn final frame rather than handing back a partial sample.
    actual = (available // bytes_per_frame) * bytes_per_frame
    if actual == info.data_size and info.data_size > 0:
        return info
    with open(path, "r+b") as fh:
        fh.seek(4)
        fh.write(struct.pack("<I", info.data_offset + actual - 8))
        fh.seek(info.data_offset - 4)
        fh.write(struct.pack("<I", actual))
        fh.flush()
    info.data_size = actual
    return info


def read_wav(path: str) -> Tuple[np.ndarray, int]:
    """Read a WAV written by :class:`WavWriter` into float32 ``(frames, ch)``."""
    info = parse_wav_header(path)
    if info is None:
        raise ValueError(f"not a readable WAV file: {path}")
    with open(path, "rb") as fh:
        fh.seek(info.data_offset)
        raw = fh.read(info.data_size)
    if info.audio_format == WAVE_FORMAT_IEEE_FLOAT and info.bits_per_sample == 32:
        data = np.frombuffer(raw, dtype="<f4")
    elif info.bits_per_sample == 16:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif info.bits_per_sample == 32:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif info.bits_per_sample == 8:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:  # pragma: no cover - unusual depth
        raise ValueError(f"unsupported bit depth: {info.bits_per_sample}")
    channels = max(1, info.channels)
    frames = data.shape[0] // channels
    return data[: frames * channels].reshape(frames, channels).astype(np.float32), info.sample_rate
