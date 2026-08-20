import io
import json
import os
import struct

import numpy as np
import pytest

from meetingcap import cli
from meetingcap.audio.synthetic import SyntheticBackend
from meetingcap.capture.mixer import mix_tracks
from meetingcap.capture.sync import DriftMonitor, SyncState
from meetingcap.capture.wavio import WavWriter, read_wav
from meetingcap.deps import Dependency, install_command, setup_instructions
from meetingcap.diagnostics import Diagnostics
from meetingcap.permissions import PermissionReport, PermissionStatus
from meetingcap.ui import console


# -- diagnostics ---------------------------------------------------------


def run_diagnostics(tmp_path, backend=None):
    stream = io.StringIO()
    diag = Diagnostics(backend or SyntheticBackend(), interactive=False,
                       stream=stream, seconds=0.4, output_dir=str(tmp_path))
    ok = diag.run()
    return ok, stream.getvalue(), diag


def test_diagnostics_pass_against_a_working_backend(tmp_path):
    ok, output, diag = run_diagnostics(tmp_path)
    assert ok, output
    for label in ("SYSTEM LOOPBACK", "MICROPHONE", "48k RESAMPLING",
                  "TIMESTAMPING", "DEVICE WATCHER", "DISK WRITER"):
        assert f"{label} " in output
    assert "PASS" in output and "FAIL" not in output
    assert "No administrator password is required" in output


def test_diagnostics_fail_on_a_silent_system_stream(tmp_path):
    class SilentBackend(SyntheticBackend):
        def create_system_source(self, device=None):
            source = super().create_system_source(device)
            source.amplitude = 0.0
            return source

    ok, output, _ = run_diagnostics(tmp_path, SilentBackend())
    assert not ok
    assert "SYSTEM LOOPBACK" in output
    assert "silent" in output and "nothing was playing?" in output


def test_diagnostics_report_an_unavailable_device(tmp_path):
    ok, output, _ = run_diagnostics(tmp_path, SyntheticBackend(available=False))
    assert not ok
    assert "PERMISSIONS" in output and "DEVICE_UNAVAILABLE" in output


def test_diagnostics_disk_writer_check_covers_the_crash_path(tmp_path):
    _, _, diag = run_diagnostics(tmp_path)
    result = next(r for r in diag.results if r.name == "DISK WRITER")
    assert result.passed and "repair OK" in result.detail


def test_diagnostics_resampling_check_is_self_contained(tmp_path):
    _, _, diag = run_diagnostics(tmp_path)
    result = next(r for r in diag.results if r.name == "48k RESAMPLING")
    assert result.passed
    assert "48000 frames" in result.detail
    measured = int(result.detail.split(",")[1].strip().split()[0])
    assert abs(measured - 440) <= 5


# -- CLI -----------------------------------------------------------------


def test_list_devices_prints_the_default_marker(capsys):
    assert cli.main(["--list-devices", "--source", "synthetic"]) == 0
    out = capsys.readouterr().out
    assert "Synthetic Speakers" in out
    assert "*" in out and "never hard-coded" in out


def test_check_reports_permissions_and_no_elevation(capsys):
    assert cli.main(["--check", "--source", "synthetic"]) == 0
    out = capsys.readouterr().out
    assert "Administrator privileges: NOT REQUIRED" in out
    assert "System audio:" in out and "Microphone:" in out


def test_record_for_a_fixed_duration_writes_a_session(tmp_path, capsys):
    code = cli.main(["--source", "synthetic", "--output", str(tmp_path),
                     "--duration", "0.5", "--non-interactive", "--title", "Standup"])
    assert code == 0
    out = capsys.readouterr().out
    assert "Recording finished." in out
    sessions = os.listdir(tmp_path)
    assert len(sessions) == 1 and sessions[0].endswith("_standup")
    directory = tmp_path / sessions[0]
    assert (directory / "system.wav").exists()
    assert (directory / "microphone.wav").exists()
    assert (directory / "meeting_mix.wav").exists()
    assert json.loads((directory / "session.json").read_text())["ended_at"]


def test_no_mix_flag_is_honoured(tmp_path):
    cli.main(["--source", "synthetic", "--output", str(tmp_path), "--duration",
              "0.4", "--non-interactive", "--no-mix"])
    directory = tmp_path / os.listdir(tmp_path)[0]
    assert not (directory / "meeting_mix.wav").exists()


def test_microphone_only_flag(tmp_path):
    cli.main(["--source", "synthetic", "--output", str(tmp_path), "--duration",
              "0.4", "--non-interactive", "--no-system"])
    directory = tmp_path / os.listdir(tmp_path)[0]
    assert (directory / "microphone.wav").exists()
    assert not (directory / "system.wav").exists()


def test_diagnostics_flag_returns_zero_on_success(capsys):
    assert cli.main(["--diagnostics", "--source", "synthetic",
                     "--non-interactive"]) == 0
    assert "PASS" in capsys.readouterr().out


def test_repair_finalises_segments_of_an_interrupted_session(tmp_path, capsys):
    audio = tmp_path / "audio"
    audio.mkdir()
    path = str(audio / "system_0001.wav")
    with WavWriter(path, 48_000, 1) as writer:
        writer.write(np.zeros((4800, 1), dtype=np.float32))
    with open(path, "r+b") as fh:                  # blank the sizes
        fh.seek(4)
        fh.write(struct.pack("<I", 0))
        fh.seek(40)
        fh.write(struct.pack("<I", 0))

    assert cli.main(["--repair", str(tmp_path)]) == 0
    assert "4800 frames" in capsys.readouterr().out
    assert read_wav(path)[0].shape[0] == 4800


def test_repair_of_a_missing_directory_is_reported(capsys):
    assert cli.main(["--repair", "/nonexistent/session"]) == 1
    assert "No such session directory" in capsys.readouterr().out


def test_blocked_permissions_stop_recording_with_an_explanation(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("meetingcap.audio.registry.get_backend",
                        lambda *a, **k: SyntheticBackend(available=False))
    monkeypatch.setattr(cli, "get_backend", lambda *a, **k: SyntheticBackend(available=False))
    code = cli.main(["--source", "synthetic", "--output", str(tmp_path),
                     "--duration", "0.2", "--non-interactive"])
    out = capsys.readouterr().out
    assert code == 1
    assert "Nothing can be captured" in out
    assert os.listdir(tmp_path) == []


def test_missing_dependency_prints_one_copyable_command(monkeypatch, capsys):
    fake = Dependency("definitely_not_installed", "SomePackage", "testing")
    monkeypatch.setattr(cli, "missing_deps", lambda *a, **k: [fake])
    monkeypatch.setattr(cli, "deps_report", lambda *a, **k:
                        "Missing dependencies:\n  - SomePackage: testing")
    assert cli.main([]) == 2
    out = capsys.readouterr().out
    assert "SomePackage" in out and "pip install" in out


def test_module_entry_point_actually_runs(tmp_path):
    """`python -m meetingcap` must call main(), not just import it."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "meetingcap", "--list-devices",
         "--source", "synthetic"],
        capture_output=True, text=True, timeout=60,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    assert result.returncode == 0, result.stderr
    assert "Synthetic Speakers" in result.stdout


def test_script_entry_point_actually_runs():
    """`python meeting_capture.py` is the invocation the spec names."""
    import subprocess
    import sys

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        [sys.executable, os.path.join(root, "meeting_capture.py"),
         "--check", "--source", "synthetic"],
        capture_output=True, text=True, timeout=60, cwd=root)
    assert result.returncode == 0, result.stderr
    assert "Administrator privileges: NOT REQUIRED" in result.stdout


def test_install_commands_never_ask_for_elevation():
    dep = Dependency("pyaudiowpatch", "PyAudioWPatch", "loopback")
    assert install_command([dep], "Windows").startswith("py -m pip install")
    for system in ("Windows", "Darwin", "Linux"):
        text = setup_instructions(system)
        assert "sudo" not in text or "No sudo is required" in text
        assert "administrator" not in text.lower() or "No administrator" in text


# -- sync, mixing and console --------------------------------------------


def test_drift_monitor_measures_a_slow_clock():
    monitor = DriftMonitor("SYSTEM", 48_000)
    # Ten blocks of 4800 frames, but the wall clock advanced 1.01 s: the
    # device clock is running slow relative to monotonic time.
    for i in range(10):
        monitor.observe(4800, int((i + 1) * 0.101 * 1e9))
    report = monitor.report()
    assert report.frames == 48_000
    assert report.drift_seconds < 0
    assert report.drift_ppm < -5000


def test_sync_state_reports_relative_start_offset():
    sync = SyncState()
    sync.observe("SYSTEM", 4800, int(1.1 * 1e9))
    sync.observe("MIC", 4800, int(1.4 * 1e9))
    assert sync.relative_offset_seconds("MIC", "SYSTEM") == pytest.approx(0.3, abs=0.01)
    assert sync.relative_offset_seconds("MIC", "NOPE") is None


def test_mix_aligns_the_tracks_using_the_measured_offset(tmp_path):
    system = str(tmp_path / "system.wav")
    mic = str(tmp_path / "microphone.wav")
    with WavWriter(system, 48_000, 1) as writer:
        writer.write(np.full((48_000, 1), 0.5, dtype=np.float32))
    with WavWriter(mic, 48_000, 1) as writer:
        writer.write(np.full((24_000, 1), 0.5, dtype=np.float32))

    out = mix_tracks(system, mic, str(tmp_path / "mix.wav"), offset_seconds=0.5)
    pcm, rate = read_wav(out)
    assert rate == 48_000
    assert pcm.shape[0] == 48_000
    # The first half second is system only, the second half has both.
    assert float(np.mean(np.abs(pcm[:20_000]))) < float(np.mean(np.abs(pcm[30_000:])))


def test_mix_handles_a_single_available_track(tmp_path):
    mic = str(tmp_path / "microphone.wav")
    with WavWriter(mic, 48_000, 1) as writer:
        writer.write(np.full((4800, 1), 0.5, dtype=np.float32))
    out = mix_tracks(None, mic, str(tmp_path / "mix.wav"))
    assert read_wav(out)[0].shape[0] == 4800
    assert mix_tracks(None, None, str(tmp_path / "none.wav")) is None


def test_banner_matches_the_specified_layout():
    reports = [PermissionReport("System audio", PermissionStatus.SUPPORTED_AND_ALLOWED),
               PermissionReport("Microphone", PermissionStatus.SUPPORTED_AND_ALLOWED)]
    text = console.render_banner("Windows WASAPI Loopback", reports,
                                 "AirPods", "AirPods Hands-Free")
    assert text.splitlines()[0] == "Meeting Capture"
    assert "System audio: Windows WASAPI Loopback  OK" in text
    assert "Output: AirPods  OK" in text
    assert "Microphone: AirPods Hands-Free  OK" in text
    assert "Administrator privileges: NOT REQUIRED  OK" in text


def test_banner_explains_a_missing_macos_permission():
    reports = [
        PermissionReport("System audio",
                         PermissionStatus.SUPPORTED_BUT_PERMISSION_MISSING,
                         "System audio capture is supported, but macOS has not "
                         "granted Screen & System Audio Recording permission.",
                         "Open: System Settings -> Privacy & Security -> "
                         "Screen & System Audio Recording"),
        PermissionReport("Microphone", PermissionStatus.SUPPORTED_AND_ALLOWED),
    ]
    text = console.render_banner("macOS ScreenCaptureKit", reports)
    assert "Screen & System Audio Recording" in text
    assert "No administrator password is required" in text
    assert "Traceback" not in text


def test_live_status_shows_recording_state_and_counters():
    from meetingcap.capture.recorder import RecorderStatus
    status = RecorderStatus(elapsed_s=2057, recording=True, system_bar="#######...",
                            mic_bar="#####.....", system_device="AirPods",
                            mic_device="AirPods", dropouts=0, device_switches=1)
    lines = console.render_status(status)
    assert lines[0] == "* RECORDING 00:34:17"
    assert "SYSTEM #######..." in lines[1]
    assert "Device switches: 1" in lines[5]
    assert "Ctrl+C" in lines[-1]
