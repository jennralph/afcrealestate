import time

import numpy as np

from meetingcap.audio.base import AudioSource, AudioSourceError, DeviceInfo
from meetingcap.capture.session import SessionMetadata
from meetingcap.capture.watcher import (Backoff, DeviceWatcher,
                                        MICROPHONE_CHANGED, SYSTEM_FORMAT_CHANGED,
                                        SYSTEM_OUTPUT_CHANGED, SYSTEM_OUTPUT_LOST)
from meetingcap.capture.watchdog import Watchdog
from meetingcap.capture.writer import TrackWriter


class FakeBackend:
    """A backend whose default endpoints can be moved from the test."""

    name = "Fake"
    system_capture_follows_default_endpoint = True

    def __init__(self):
        self.output = DeviceInfo("0", "Realtek Speakers", "output", 48_000, 2, True)
        self.input = DeviceInfo("1", "Laptop Microphone", "input", 48_000, 1, True)
        self.raise_on_output = False

    def default_output(self):
        if self.raise_on_output:
            raise OSError("endpoint is being torn down")
        return self.output

    def default_input(self):
        return self.input


class FakeSource(AudioSource):
    kind = "SYSTEM"

    def __init__(self, kind="SYSTEM", fail_starts=0):
        self.kind = kind
        super().__init__()
        self.opens = 0
        self.closes = 0
        self.fail_starts = fail_starts

    def _open(self):
        self.opens += 1
        if self.fail_starts > 0:
            self.fail_starts -= 1
            raise AudioSourceError("device busy")

    def _close(self):
        self.closes += 1

    def emit(self, frames=480):
        self._emit(np.zeros((frames, 1), dtype=np.float32), 48_000, 1, "Fake Device")


# -- backoff -------------------------------------------------------------


def test_backoff_follows_the_specified_schedule():
    backoff = Backoff()
    assert [backoff.next_delay() for _ in range(7)] == [0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 5.0]
    backoff.reset()
    assert backoff.next_delay() == 0.1


# -- device watcher ------------------------------------------------------


def test_watcher_reports_an_output_switch_to_airpods():
    backend = FakeBackend()
    seen = []
    watcher = DeviceWatcher(backend, seen.append)
    watcher.prime()
    watcher.poll()
    assert seen == []                                   # nothing changed yet

    backend.output = DeviceInfo("2", "AirPods", "output", 48_000, 2, True)
    watcher.poll()
    assert len(seen) == 1
    change = seen[0]
    assert change.type == SYSTEM_OUTPUT_CHANGED
    assert change.from_device == "Realtek Speakers"
    assert change.to_device == "AirPods"
    assert change.requires_reopen


def test_watcher_reports_a_microphone_switch():
    backend = FakeBackend()
    seen = []
    watcher = DeviceWatcher(backend, seen.append)
    watcher.prime()
    backend.input = DeviceInfo("3", "USB Headset Mic", "input", 44_100, 1, True)
    watcher.poll()
    assert [c.type for c in seen] == [MICROPHONE_CHANGED]


def test_watcher_reports_a_sample_rate_change_as_a_format_change():
    """A Bluetooth profile switch keeps the name but changes the rate."""
    backend = FakeBackend()
    seen = []
    watcher = DeviceWatcher(backend, seen.append)
    watcher.prime()
    backend.output = DeviceInfo("0", "Realtek Speakers", "output", 16_000, 1, True)
    watcher.poll()
    assert [c.type for c in seen] == [SYSTEM_FORMAT_CHANGED]
    assert seen[0].to_sample_rate == 16_000


def test_watcher_reports_a_disappearing_device():
    backend = FakeBackend()
    seen = []
    watcher = DeviceWatcher(backend, seen.append)
    watcher.prime()
    backend.output = None
    watcher.poll()
    assert [c.type for c in seen] == [SYSTEM_OUTPUT_LOST]


def test_watcher_survives_an_enumeration_error():
    backend = FakeBackend()
    seen = []
    watcher = DeviceWatcher(backend, seen.append)
    watcher.prime()
    backend.raise_on_output = True
    watcher.poll()
    assert watcher.poll_errors == 1
    assert [c.type for c in seen] == [SYSTEM_OUTPUT_LOST]
    backend.raise_on_output = False
    watcher.poll()               # recovers on the next pass


def test_a_handler_that_raises_does_not_kill_the_watcher():
    backend = FakeBackend()

    def boom(change):
        raise RuntimeError("handler exploded")

    watcher = DeviceWatcher(backend, boom)
    watcher.prime()
    backend.output = DeviceInfo("2", "AirPods", "output", 48_000, 2, True)
    watcher.poll()               # must not raise


# -- watchdog ------------------------------------------------------------


def make_watchdog(tmp_path, source, **kwargs):
    session = SessionMetadata(str(tmp_path))
    writer = TrackWriter("SYSTEM", str(tmp_path / "audio"), session)
    watchdog = Watchdog(str(tmp_path), **kwargs)
    watchdog.watch("SYSTEM", source, writer)
    return watchdog, writer


def test_watchdog_restarts_a_dead_stream(tmp_path):
    source = FakeSource()
    watchdog, _ = make_watchdog(tmp_path, source)
    source.start()
    source.emit()
    watchdog.check()
    assert source.opens == 1                      # healthy: untouched

    source._set_error("AUDCLNT_E_DEVICE_INVALIDATED")
    outages = []
    watchdog.on_outage = lambda name, reason: outages.append((name, reason))
    watchdog.check()
    assert source.opens == 2                      # restarted in place
    assert outages and "AUDCLNT_E_DEVICE_INVALIDATED" in outages[0][1]


def test_watchdog_treats_a_silent_stream_as_stalled(tmp_path):
    source = FakeSource()
    watchdog, _ = make_watchdog(tmp_path, source, stall_timeout=0.05)
    source.start()
    source.emit()
    time.sleep(0.1)
    watchdog.check()
    assert source.opens == 2


def test_watchdog_backs_off_between_restart_attempts(tmp_path):
    source = FakeSource(fail_starts=99)
    watchdog, _ = make_watchdog(tmp_path, source)
    try:
        source.start()
    except AudioSourceError:
        pass
    watchdog.check()
    first = source.opens
    watchdog.check()                              # too soon: still backing off
    assert source.opens == first


def test_watchdog_gives_up_on_a_non_recoverable_failure(tmp_path):
    class PermissionDenied(FakeSource):
        def _open(self):
            self.opens += 1
            raise AudioSourceError("permission not granted", recoverable=False)

    source = PermissionDenied()
    watchdog, _ = make_watchdog(tmp_path, source)
    watchdog.check()
    opens = source.opens
    watchdog.check()
    watchdog.check()
    assert source.opens == opens                  # not retried forever


def test_watchdog_reports_recovery(tmp_path):
    source = FakeSource()
    recoveries = []
    watchdog, _ = make_watchdog(tmp_path, source,
                                on_recovery=lambda n, r: recoveries.append(r))
    source.start()
    source._set_error("device lost")
    watchdog.check()
    source.emit()
    watchdog.check()
    assert recoveries and "recovered" in recoveries[0]


def test_suspended_track_is_left_alone(tmp_path):
    source = FakeSource()
    watchdog, _ = make_watchdog(tmp_path, source)
    source.start()
    source._set_error("device lost")
    watchdog.suspend("SYSTEM")
    watchdog.check()
    assert source.opens == 1
    watchdog.resume("SYSTEM")
    watchdog.check()
    assert source.opens == 2


def test_health_dictionary_has_the_specified_keys(tmp_path):
    source = FakeSource()
    watchdog, writer = make_watchdog(tmp_path, source)
    source.start()
    source.emit()
    health = watchdog.health()
    assert health["system_audio"] is True
    assert isinstance(health["system_last_packet_ms"], int)
    assert health["queue_overruns"] == 0
    assert "disk_free_mb" in health
