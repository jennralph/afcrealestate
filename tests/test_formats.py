import math

import numpy as np
import pytest

from meetingcap.audio.base import AudioChunk
from meetingcap.audio.formats import (Normalizer, Resampler, TARGET_SAMPLE_RATE,
                                      dbfs, deinterleave, peak_dbfs, to_float32,
                                      to_mono)


def sine(frames, rate, freq=440.0, channels=1, phase=0.0, amplitude=1.0):
    idx = np.arange(frames, dtype=np.float32)
    wave = amplitude * np.sin(phase + 2 * math.pi * freq / rate * idx)
    return np.repeat(wave.astype(np.float32).reshape(-1, 1), channels, axis=1)


def test_to_float32_converts_int16_range():
    pcm = np.array([-32768, 0, 32767], dtype=np.int16)
    out = to_float32(pcm)
    assert out.dtype == np.float32
    assert out[0] == pytest.approx(-1.0)
    assert out[1] == pytest.approx(0.0)
    assert out[2] == pytest.approx(1.0, abs=1e-4)


def test_deinterleave_splits_channels():
    raw = np.array([1, 2, 3, 4, 5, 6], dtype=np.float32)
    out = deinterleave(raw, 2)
    assert out.shape == (3, 2)
    assert list(out[1]) == [3.0, 4.0]


def test_to_mono_averages_channels():
    pcm = np.array([[1.0, -1.0], [0.5, 0.5]], dtype=np.float32)
    out = to_mono(pcm)
    assert out.shape == (2, 1)
    assert out[0, 0] == pytest.approx(0.0)
    assert out[1, 0] == pytest.approx(0.5)


def test_dbfs_and_peak_of_silence_and_full_scale():
    assert dbfs(np.zeros(480, dtype=np.float32)) == -120.0
    full = np.ones(480, dtype=np.float32)
    assert dbfs(full) == pytest.approx(0.0, abs=0.01)
    assert peak_dbfs(full) == pytest.approx(0.0, abs=0.01)


def test_resampler_preserves_duration_within_one_percent():
    resampler = Resampler(44_100, 48_000, 1)
    produced = 0
    for _ in range(100):                       # 100 blocks of 10 ms
        produced += resampler.process(sine(441, 44_100)).shape[0]
    assert abs(produced - 48_000) < 480        # within 1%


def test_resampler_is_continuous_across_block_boundaries():
    """No click at the seam: consecutive blocks must join smoothly."""
    rate, freq = 44_100, 200.0
    resampler = Resampler(rate, 48_000, 1)
    phase, blocks = 0.0, []
    step = 2 * math.pi * freq / rate
    for _ in range(20):
        idx = np.arange(441, dtype=np.float32)
        blocks.append(resampler.process(
            np.sin(phase + step * idx).astype(np.float32).reshape(-1, 1)))
        phase = (phase + step * 441) % (2 * math.pi)
    out = np.concatenate(blocks, axis=0)[:, 0]
    # A discontinuity would show up as a step far larger than one sample of
    # a 200 Hz tone at 48 kHz (~0.026 peak-to-peak).
    assert float(np.max(np.abs(np.diff(out)))) < 0.1


def test_resampler_passthrough_when_rates_match():
    resampler = Resampler(48_000, 48_000, 1)
    block = sine(480, 48_000)
    assert resampler.process(block).shape == (480, 1)


def test_normalizer_resamples_and_limits_channels():
    normalizer = Normalizer("MIC", max_channels=1)
    chunk = AudioChunk(source="MIC", pcm=sine(160, 16_000, channels=2),
                       timestamp_monotonic=1_000_000_000, frame_count=160,
                       sample_rate=16_000, channels=2, sequence_number=1,
                       device="USB mic")
    block = normalizer.process(chunk)
    assert block.sample_rate == TARGET_SAMPLE_RATE
    assert block.pcm.shape[1] == 1
    assert block.source_sample_rate == 16_000
    assert block.frame_count == pytest.approx(480, abs=5)


def test_normalizer_keeps_system_stereo():
    normalizer = Normalizer("SYSTEM", max_channels=2)
    chunk = AudioChunk(source="SYSTEM", pcm=sine(480, 48_000, channels=2),
                       timestamp_monotonic=1, frame_count=480,
                       sample_rate=48_000, channels=2, sequence_number=1)
    assert normalizer.process(chunk).pcm.shape[1] == 2


def test_normalizer_handles_a_sample_rate_change_mid_stream():
    """A Bluetooth profile switch changes the rate under us."""
    normalizer = Normalizer("MIC", max_channels=1)
    first = AudioChunk("MIC", sine(480, 48_000), 1, 480, 48_000, 1, 1)
    second = AudioChunk("MIC", sine(160, 16_000), 2, 160, 16_000, 1, 2)
    assert normalizer.process(first).frame_count == 480
    out = normalizer.process(second)
    assert out.sample_rate == TARGET_SAMPLE_RATE
    assert out.frame_count == pytest.approx(480, abs=5)
