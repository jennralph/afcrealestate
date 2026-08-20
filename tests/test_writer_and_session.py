import json
import os
import time

import numpy as np
import pytest

from meetingcap.audio.formats import NormalizedBlock
from meetingcap.capture.session import SessionMetadata
from meetingcap.capture.wavio import read_wav
from meetingcap.capture.writer import TrackWriter, concat_segments


def block(frames=4800, channels=1, start_ns=0, sample_rate=48_000, source="SYSTEM"):
    pcm = np.full((frames, channels), 0.25, dtype=np.float32)
    end_ns = start_ns + int(frames / sample_rate * 1e9)
    return NormalizedBlock(pcm=pcm, sample_rate=sample_rate, channels=channels,
                           timestamp_monotonic=end_ns, source=source,
                           sequence_number=1)


def drain(writer, expected_frames, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if writer.stats().frames_written >= expected_frames:
            return True
        time.sleep(0.02)
    return False


# -- session metadata ----------------------------------------------------


def test_session_json_has_the_specified_shape(tmp_path):
    session = SessionMetadata(str(tmp_path))
    data = json.loads((tmp_path / "session.json").read_text())
    for key in ("session_id", "started_at", "platform", "system_devices",
                "microphone_devices", "device_changes", "dropouts",
                "sample_rates", "ended_at"):
        assert key in data
    assert data["ended_at"] is None
    assert data["administrator_required"] is False
    session.finish()
    assert json.loads((tmp_path / "session.json").read_text())["ended_at"]


def test_device_change_event_shape(tmp_path):
    session = SessionMetadata(str(tmp_path))
    event = session.add_device_change("system_output_changed",
                                      "Realtek Speakers", "AirPods")
    assert event["type"] == "system_output_changed"
    assert event["from"] == "Realtek Speakers" and event["to"] == "AirPods"
    assert isinstance(event["time"], float)
    assert session.snapshot()["device_changes"] == [event]


def test_note_device_is_idempotent_and_records_sample_rate(tmp_path):
    session = SessionMetadata(str(tmp_path))
    session.note_device("SYSTEM", "AirPods", 48_000, 2)
    session.note_device("SYSTEM", "AirPods", 48_000, 2)
    data = session.snapshot()
    assert len(data["system_devices"]) == 1
    assert data["sample_rates"]["AirPods"] == 48_000


def test_save_is_atomic_and_leaves_no_temp_file(tmp_path):
    session = SessionMetadata(str(tmp_path))
    session.add_dropout("MIC", "device removed", 0.4)
    assert not os.path.exists(str(tmp_path / "session.json.tmp"))
    assert json.loads((tmp_path / "session.json").read_text())["dropouts"]


# -- track writer --------------------------------------------------------


def test_writer_rotates_segments(tmp_path):
    session = SessionMetadata(str(tmp_path))
    writer = TrackWriter("SYSTEM", str(tmp_path / "audio"), session,
                         rotate_seconds=0.1)
    writer.start()
    try:
        start = 0
        for _ in range(4):
            writer.submit(block(4800, start_ns=start))   # 100 ms each
            start += int(0.1 * 1e9)
            time.sleep(0.05)
        assert drain(writer, 4 * 4800)
    finally:
        writer.stop()
    files = sorted(os.listdir(tmp_path / "audio"))
    assert len(files) >= 3
    assert files[0] == "system_0001.wav"
    assert [s["source"] for s in session.snapshot()["segments"]] == ["SYSTEM"] * len(files)


def test_writer_pads_a_device_switch_gap_with_silence(tmp_path):
    """The tracks must stay on one timeline across an interruption."""
    session = SessionMetadata(str(tmp_path))
    writer = TrackWriter("MIC", str(tmp_path / "audio"), session,
                         rotate_seconds=3600)
    writer.start()
    try:
        writer.submit(block(4800, start_ns=0))                    # 0.0 - 0.1s
        writer.submit(block(4800, start_ns=int(1.1 * 1e9)))       # 1.1 - 1.2s
        assert drain(writer, int(1.2 * 48_000))
    finally:
        writer.stop()
    pcm, _ = read_wav(str(tmp_path / "audio" / "mic_0001.wav"))
    assert pcm.shape[0] == pytest.approx(int(1.2 * 48_000), abs=48)
    silent = pcm[4800 + 100: 4800 + 4700]
    assert float(np.max(np.abs(silent))) == pytest.approx(0.0, abs=1e-4)
    assert writer.stats().silence_padded_s == pytest.approx(1.0, abs=0.01)


def test_writer_ignores_sub_50ms_jitter(tmp_path):
    session = SessionMetadata(str(tmp_path))
    writer = TrackWriter("MIC", str(tmp_path / "audio"), session, rotate_seconds=3600)
    writer.start()
    try:
        writer.submit(block(480, start_ns=0))
        writer.submit(block(480, start_ns=int(0.02 * 1e9)))   # 10 ms late
        assert drain(writer, 960)
    finally:
        writer.stop()
    assert writer.stats().silence_padded_s == 0.0


def test_writer_starts_a_new_segment_when_channel_count_changes(tmp_path):
    session = SessionMetadata(str(tmp_path))
    writer = TrackWriter("SYSTEM", str(tmp_path / "audio"), session, rotate_seconds=3600)
    writer.start()
    try:
        writer.submit(block(480, channels=2, start_ns=0))
        time.sleep(0.05)
        writer.submit(block(480, channels=1, start_ns=int(0.01 * 1e9)))
        assert drain(writer, 960)
    finally:
        writer.stop()
    files = sorted(os.listdir(tmp_path / "audio"))
    assert files == ["system_0001.wav", "system_0002.wav"]
    assert read_wav(str(tmp_path / "audio" / files[0]))[0].shape[1] == 2
    assert read_wav(str(tmp_path / "audio" / files[1]))[0].shape[1] == 1


def test_submit_never_blocks_and_counts_overruns(tmp_path):
    session = SessionMetadata(str(tmp_path))
    writer = TrackWriter("MIC", str(tmp_path / "audio"), session, queue_blocks=4)
    # Deliberately not started: the queue can only fill up.
    for _ in range(20):
        writer.submit(block(480))
    assert writer.stats().queue_overruns >= 15


def test_concat_segments_joins_in_order(tmp_path):
    session = SessionMetadata(str(tmp_path))
    writer = TrackWriter("SYSTEM", str(tmp_path / "audio"), session, rotate_seconds=0.05)
    writer.start()
    try:
        start = 0
        for _ in range(3):
            writer.submit(block(4800, start_ns=start))
            start += int(0.1 * 1e9)
            time.sleep(0.08)
        assert drain(writer, 3 * 4800)
    finally:
        writer.stop()
    out = concat_segments(writer.segment_files, str(tmp_path / "system.wav"))
    pcm, rate = read_wav(out)
    assert rate == 48_000
    assert pcm.shape[0] == pytest.approx(3 * 4800, abs=48)


def test_concat_returns_none_without_segments(tmp_path):
    assert concat_segments([], str(tmp_path / "empty.wav")) is None
