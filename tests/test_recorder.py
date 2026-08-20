"""End-to-end recorder tests against the synthetic backend.

These exercise the acceptance criteria that do not need real hardware:
separate tracks (A/B), continuation across a device switch (C/D), crash
survival (G), permission reporting instead of a stack trace (H), and the
``on_audio_chunk`` boundary for the later transcription phase.
"""

import json
import os
import time

import numpy as np
import pytest

from meetingcap import MIC, SYSTEM
from meetingcap.audio.base import AudioSourceError
from meetingcap.audio.synthetic import SyntheticBackend
from meetingcap.capture.recorder import Recorder, RecorderConfig
from meetingcap.capture.wavio import read_wav, repair_wav
from meetingcap.permissions import PermissionStatus


@pytest.fixture
def config(tmp_path):
    return RecorderConfig(output_dir=str(tmp_path), backend_name="synthetic",
                          watcher_interval=0.05, watchdog_interval=0.05,
                          stall_timeout=1.0, rotate_seconds=3600)


def record_for(recorder, seconds=0.6):
    recorder.start()
    time.sleep(seconds)
    return recorder.stop()


def test_records_two_independent_tracks(config):
    """Requirements A and B: remote voice in system.wav, my voice in microphone.wav."""
    result = record_for(Recorder(config, backend=SyntheticBackend()))

    assert os.path.exists(result.system_wav)
    assert os.path.exists(result.microphone_wav)
    system, rate = read_wav(result.system_wav)
    mic, _ = read_wav(result.microphone_wav)
    assert rate == 48_000
    assert system.shape[0] > 48_000 * 0.3
    assert mic.shape[0] > 48_000 * 0.3
    # The two tracks carry different signals: they are not one summed stream.
    assert float(np.max(np.abs(system))) > 0.1
    assert float(np.max(np.abs(mic))) > 0.05
    assert not np.allclose(system[:1000, 0], mic[:1000, 0], atol=0.01)


def test_writes_a_mix_when_asked_and_not_otherwise(config, tmp_path):
    result = record_for(Recorder(config, backend=SyntheticBackend()))
    assert result.mix_wav and os.path.exists(result.mix_wav)

    config.mix = False
    result = record_for(Recorder(config, backend=SyntheticBackend()))
    assert result.mix_wav is None


def test_session_json_records_devices_and_finishes(config):
    recorder = Recorder(config, backend=SyntheticBackend())
    result = record_for(recorder)
    data = json.loads(open(os.path.join(result.directory, "session.json")).read())
    assert data["ended_at"] is not None
    assert data["administrator_required"] is False
    assert [d["name"] for d in data["system_devices"]] == ["Synthetic Speakers"]
    assert [d["name"] for d in data["microphone_devices"]] == ["Synthetic Microphone"]
    assert data["drift"]["SYSTEM"]["frames"] > 0
    assert data["tracks"]["system"] == "system.wav"


def test_segments_survive_an_abrupt_death(config):
    """Requirement G: only the current buffer may be lost."""
    recorder = Recorder(config, backend=SyntheticBackend())
    directory = recorder.start()
    time.sleep(0.5)
    # Simulate the process vanishing: stop the sources and pumps without any
    # finalisation, exactly as a kill -9 would.
    for track in recorder.tracks.values():
        if track.source:
            track.source.stop()
        if track.pipeline:
            track.pipeline.stop()
    recorder.watcher.stop()
    recorder.watchdog.stop()
    for track in recorder.tracks.values():
        if track.writer:
            track.writer._stop_evt.set()
            track.writer.join(timeout=2)
            # The segment header was never patched; repair must recover it.
            for path in track.writer.segment_files:
                info = repair_wav(path)
                assert info is not None and info.frame_count > 0

    segments = os.listdir(os.path.join(directory, "audio"))
    assert any(name.startswith("system_") for name in segments)
    assert any(name.startswith("mic_") for name in segments)


def test_recording_continues_across_a_device_switch(config):
    """Requirements C and D: switching output/input must not end the session."""
    backend = SyntheticBackend()
    recorder = Recorder(config, backend=backend)
    recorder.start()
    time.sleep(0.3)

    backend.output_device = "AirPods"          # user switches to Bluetooth
    backend.input_device = "AirPods Hands-Free"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if recorder.tracks[SYSTEM].device == "AirPods":
            break
        time.sleep(0.05)
    time.sleep(0.3)
    result = recorder.stop()

    data = json.loads(open(os.path.join(result.directory, "session.json")).read())
    types = {event["type"] for event in data["device_changes"]}
    assert "system_output_changed" in types
    assert recorder.tracks[SYSTEM].device == "AirPods"
    assert {d["name"] for d in data["system_devices"]} == {"Synthetic Speakers", "AirPods"}
    # One logical meeting: a single set of tracks, still readable.
    assert read_wav(result.system_wav)[0].shape[0] > 48_000 * 0.3
    assert result.dropouts >= 1
    recovered = [d for d in data["dropouts"] if d.get("recovered")]
    assert recovered and recovered[0]["duration_s"] < 2.0     # spec 6 target


def test_watchdog_restarts_a_stream_that_dies_mid_session(config, monkeypatch):
    backend = SyntheticBackend()
    recorder = Recorder(config, backend=backend)
    recorder.start()
    time.sleep(0.2)
    source = recorder.tracks[MIC].source
    source._set_error("synthetic stream invalidated")

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if recorder.tracks[MIC].source.health().alive:
            break
        time.sleep(0.05)
    result = recorder.stop()
    data = json.loads(open(os.path.join(result.directory, "session.json")).read())
    assert any("invalidated" in d["reason"] for d in data["dropouts"])
    assert read_wav(result.microphone_wav)[0].shape[0] > 0


def test_on_audio_chunk_subscribers_receive_mono_at_48k(config):
    """Spec 22: the transcription boundary, with no STT backend involved."""
    received = []
    recorder = Recorder(config, backend=SyntheticBackend())
    recorder.subscribe(lambda source, pcm, ts: received.append((source, pcm, ts)))
    record_for(recorder, 0.4)

    assert received
    sources = {item[0] for item in received}
    assert sources == {SYSTEM, MIC}
    for _, pcm, ts in received[:20]:
        assert pcm.ndim == 1                    # mono for transcription
        assert pcm.dtype == np.float32
        assert ts > 0
    stamps = [item[2] for item in received if item[0] == SYSTEM]
    assert stamps == sorted(stamps)             # monotonic


def test_a_subscriber_that_raises_does_not_break_recording(config):
    recorder = Recorder(config, backend=SyntheticBackend())
    recorder.subscribe(lambda *args: (_ for _ in ()).throw(RuntimeError("boom")))
    result = record_for(recorder, 0.4)
    assert read_wav(result.system_wav)[0].shape[0] > 0
    assert recorder.tracks[SYSTEM].pipeline.subscriber_errors > 0


def test_microphone_only_when_system_capture_is_unavailable(config):
    class NoSystemBackend(SyntheticBackend):
        def create_system_source(self, device=None):
            raise AudioSourceError("Screen & System Audio Recording permission "
                                   "is not granted", recoverable=False)

    recorder = Recorder(config, backend=NoSystemBackend())
    result = record_for(recorder, 0.4)
    assert result.system_wav is None
    assert result.microphone_wav and os.path.exists(result.microphone_wav)
    data = json.loads(open(os.path.join(result.directory, "session.json")).read())
    assert "permission" in data["system_error"]


def test_start_fails_cleanly_when_nothing_can_be_opened(config):
    recorder = Recorder(config, backend=SyntheticBackend(available=False))
    with pytest.raises(AudioSourceError) as excinfo:
        recorder.start()
    assert "no audio track could be opened" in str(excinfo.value)


def test_status_reports_meters_and_devices(config):
    recorder = Recorder(config, backend=SyntheticBackend())
    recorder.start()
    time.sleep(0.4)
    status = recorder.status()
    recorder.stop()
    assert status.recording
    assert status.system_device == "Synthetic Speakers"
    assert status.mic_device == "Synthetic Microphone"
    assert "#" in status.system_bar            # a tone registers on the meter
    assert status.elapsed_s > 0


def test_macos_style_backend_does_not_reopen_on_an_output_change(config):
    """ScreenCaptureKit taps the mix, so an endpoint change is informational."""

    class MixTapBackend(SyntheticBackend):
        system_capture_follows_default_endpoint = False

    backend = MixTapBackend()
    recorder = Recorder(config, backend=backend)
    recorder.start()
    time.sleep(0.2)
    original = recorder.tracks[SYSTEM].source
    backend.output_device = "External Display"
    time.sleep(0.5)
    result = recorder.stop()

    assert recorder.tracks[SYSTEM].source is original      # no reopen
    data = json.loads(open(os.path.join(result.directory, "session.json")).read())
    assert any(e["type"] == "system_output_changed" for e in data["device_changes"])


def test_drift_is_measured_against_the_monotonic_clock(config):
    result = record_for(Recorder(config, backend=SyntheticBackend()), 0.6)
    assert set(result.drift) == {SYSTEM, MIC}
    for report in result.drift.values():
        assert report["frames"] > 0
        assert abs(report["drift_seconds"]) < 0.5


def test_session_directory_uses_the_title(tmp_path):
    config = RecorderConfig(output_dir=str(tmp_path), backend_name="synthetic",
                            title="ACME Management Call")
    recorder = Recorder(config, backend=SyntheticBackend())
    directory = recorder.start()
    recorder.stop()
    assert os.path.basename(directory).endswith("_acme_management_call")


def test_permission_reports_are_structured_not_stack_traces():
    """Requirement H."""
    backend = SyntheticBackend(available=False)
    reports = backend.check_permissions()
    assert [r.status for r in reports] == [PermissionStatus.DEVICE_UNAVAILABLE] * 2
    rendered = reports[0].render()
    assert "No administrator password is required" in rendered
