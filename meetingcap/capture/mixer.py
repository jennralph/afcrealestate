"""Optional post-session mix (spec 10, 11, 12, 13).

Mixing happens *after* capture, never in a callback, and it is where the two
independent clocks are reconciled: each track is placed on a common timeline
using the monotonic start timestamp recorded for its first segment.

The tracks are not summed at equal volume.  When the user is on speakers the
microphone also hears the remote participants, so an equal-weight sum
double-counts them; the mix is attenuated and exists only for convenience
listening.  ``system.wav`` and ``microphone.wav`` remain the sources of truth
for transcription, where SYSTEM is "others" and MIC is "me" (spec 13).
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

import numpy as np

from .wavio import WavWriter, read_wav

SYSTEM_GAIN = 0.7
MIC_GAIN = 0.7


def mix_tracks(system_path: Optional[str], mic_path: Optional[str], out_path: str,
               *, offset_seconds: float = 0.0, fmt: str = "int16",
               system_gain: float = SYSTEM_GAIN,
               mic_gain: float = MIC_GAIN) -> Optional[str]:
    """Write ``meeting_mix.wav`` from the two finalised tracks.

    ``offset_seconds`` is how much later MIC started than SYSTEM (negative if
    it started earlier), taken from the monotonic timestamps captured during
    the session.
    """
    system = _load(system_path)
    mic = _load(mic_path)
    if system is None and mic is None:
        return None

    rate = (system or mic)[1]
    sys_pcm = _mono(system[0]) if system else np.zeros((0, 1), dtype=np.float32)
    mic_pcm = _mono(mic[0]) if mic else np.zeros((0, 1), dtype=np.float32)

    sys_start = 0
    mic_start = int(round(offset_seconds * rate))
    if mic_start < 0:
        sys_start, mic_start = -mic_start, 0

    total = max(sys_start + sys_pcm.shape[0], mic_start + mic_pcm.shape[0])
    if total <= 0:
        return None
    out = np.zeros((total, 1), dtype=np.float32)
    if sys_pcm.size:
        out[sys_start:sys_start + sys_pcm.shape[0]] += sys_pcm * system_gain
    if mic_pcm.size:
        out[mic_start:mic_start + mic_pcm.shape[0]] += mic_pcm * mic_gain

    peak = float(np.max(np.abs(out))) if out.size else 0.0
    if peak > 1.0:
        out /= peak            # avoid clipping rather than hard-limiting
    with WavWriter(out_path, rate, 1, fmt=fmt) as writer:
        writer.write(out)
    return out_path


def _load(path: Optional[str]) -> Optional[Tuple[np.ndarray, int]]:
    if not path or not os.path.exists(path):
        return None
    try:
        pcm, rate = read_wav(path)
    except ValueError:
        return None
    return (pcm, rate) if pcm.size else None


def _mono(pcm: np.ndarray) -> np.ndarray:
    if pcm.ndim == 1:
        return pcm.reshape(-1, 1)
    if pcm.shape[1] == 1:
        return pcm
    return pcm.mean(axis=1, keepdims=True).astype(np.float32)
