import os
import struct

import numpy as np
import pytest

from meetingcap.capture.wavio import (WavWriter, parse_wav_header, read_wav,
                                      repair_wav)


def tone(frames=4800, channels=1):
    idx = np.arange(frames, dtype=np.float32)
    wave = 0.5 * np.sin(2 * np.pi * 440.0 / 48_000 * idx)
    return np.repeat(wave.astype(np.float32).reshape(-1, 1), channels, axis=1)


@pytest.mark.parametrize("fmt", ["int16", "float32"])
def test_write_then_read_roundtrip(tmp_path, fmt):
    path = str(tmp_path / f"probe-{fmt}.wav")
    data = tone()
    with WavWriter(path, 48_000, 1, fmt=fmt) as writer:
        writer.write(data)
    pcm, rate = read_wav(path)
    assert rate == 48_000
    assert pcm.shape == (4800, 1)
    assert np.allclose(pcm, data, atol=1e-3)


def test_stereo_roundtrip(tmp_path):
    path = str(tmp_path / "stereo.wav")
    data = tone(channels=2)
    with WavWriter(path, 48_000, 2, fmt="float32") as writer:
        writer.write(data)
    pcm, rate = read_wav(path)
    assert pcm.shape == (4800, 2)


def test_mono_block_is_widened_to_the_declared_channel_count(tmp_path):
    path = str(tmp_path / "widen.wav")
    with WavWriter(path, 48_000, 2, fmt="int16") as writer:
        writer.write(tone(480, channels=1))
    pcm, _ = read_wav(path)
    assert pcm.shape == (480, 2)
    assert np.allclose(pcm[:, 0], pcm[:, 1], atol=1e-3)


def test_repair_recovers_a_segment_left_open_by_a_crash(tmp_path):
    """Requirement G: a killed process must not cost the whole meeting."""
    path = str(tmp_path / "torn.wav")
    with WavWriter(path, 48_000, 1, fmt="int16") as writer:
        writer.write(tone())
    info = parse_wav_header(path)
    with open(path, "r+b") as fh:                 # blank the sizes
        fh.seek(4)
        fh.write(struct.pack("<I", 0))
        fh.seek(info.data_offset - 4)
        fh.write(struct.pack("<I", 0))

    assert parse_wav_header(path).data_size == 0
    repaired = repair_wav(path)
    assert repaired is not None
    assert repaired.frame_count == 4800
    pcm, rate = read_wav(path)
    assert pcm.shape == (4800, 1) and rate == 48_000


def test_repair_truncates_a_torn_final_frame(tmp_path):
    path = str(tmp_path / "partial.wav")
    with WavWriter(path, 48_000, 2, fmt="int16") as writer:
        writer.write(tone(480, channels=2))
    with open(path, "ab") as fh:
        fh.write(b"\x01\x02\x03")                 # half a frame of garbage
    info = repair_wav(path)
    assert info.frame_count == 480                # partial frame dropped


def test_repair_is_idempotent_on_a_healthy_file(tmp_path):
    path = str(tmp_path / "healthy.wav")
    with WavWriter(path, 48_000, 1) as writer:
        writer.write(tone())
    first = repair_wav(path)
    second = repair_wav(path)
    assert first.data_size == second.data_size


def test_parse_returns_none_for_a_non_wav_file(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("not audio")
    assert parse_wav_header(str(path)) is None
    with pytest.raises(ValueError):
        read_wav(str(path))


def test_float32_header_declares_ieee_float(tmp_path):
    path = str(tmp_path / "f32.wav")
    with WavWriter(path, 48_000, 1, fmt="float32") as writer:
        writer.write(tone(480))
    info = parse_wav_header(path)
    assert info.audio_format == 3 and info.bits_per_sample == 32
    assert info.frame_count == 480


def test_sync_persists_data_before_close(tmp_path):
    path = str(tmp_path / "synced.wav")
    writer = WavWriter(path, 48_000, 1)
    writer.write(tone(480))
    writer.sync()
    assert os.path.getsize(path) > 44
    writer.close()
