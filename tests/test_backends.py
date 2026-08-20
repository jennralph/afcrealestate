"""Backend logic that can be tested off-platform.

The device-resolution rules are where a recorder silently records the wrong
thing, so they are exercised against fakes even on a machine that has neither
WASAPI nor PipeWire.
"""

import pytest

from meetingcap.audio import linux as linux_mod
from meetingcap.audio import windows as win
from meetingcap.audio import windows_process_loopback as wpl
from meetingcap.audio.backend import UnsupportedBackend
from meetingcap.audio.base import AudioSourceError
from meetingcap.audio.registry import get_backend, reset_cache
from meetingcap.audio.synthetic import SyntheticBackend
from meetingcap.permissions import PermissionStatus, check_permissions, summarize


# -- Windows loopback resolution -----------------------------------------


class FakePyAudio:
    """Enough of the PyAudioWPatch surface to test device resolution."""

    def __init__(self, devices, default_output=None, default_input=None,
                 has_default_loopback=False):
        self.devices = devices
        self._default_output = default_output
        self._default_input = default_input
        self.has_default_loopback = has_default_loopback
        if has_default_loopback:
            self.get_default_wasapi_loopback = self._default_loopback

    def _default_loopback(self):
        return next(d for d in self.devices if d.get("isLoopbackDevice"))

    def get_device_count(self):
        return len(self.devices)

    def get_device_info_by_index(self, index):
        return self.devices[index]

    def get_loopback_device_info_generator(self):
        return (d for d in self.devices if d.get("isLoopbackDevice"))

    def get_default_output_device_info(self):
        if self._default_output is None:
            raise OSError("no default output")
        return self.devices[self._default_output]

    def get_default_input_device_info(self):
        if self._default_input is None:
            raise OSError("no default input")
        return self.devices[self._default_input]


def device(index, name, *, loopback=False, inputs=0, outputs=0, rate=48_000):
    return {"index": index, "name": name, "isLoopbackDevice": loopback,
            "maxInputChannels": inputs, "maxOutputChannels": outputs,
            "defaultSampleRate": rate}


def test_loopback_matches_the_current_default_output():
    devices = [
        device(0, "Realtek Speakers", outputs=2),
        device(1, "AirPods", outputs=2),
        device(2, "Realtek Speakers [Loopback]", loopback=True, inputs=2),
        device(3, "AirPods [Loopback]", loopback=True, inputs=2),
    ]
    pa = FakePyAudio(devices, default_output=1)
    assert win.resolve_loopback_device(pa)["name"] == "AirPods [Loopback]"


def test_loopback_prefers_the_libraries_own_default_when_available():
    devices = [device(0, "Speakers [Loopback]", loopback=True, inputs=2)]
    pa = FakePyAudio(devices, has_default_loopback=True)
    assert win.resolve_loopback_device(pa)["name"] == "Speakers [Loopback]"


def test_loopback_honours_an_explicit_device_request():
    devices = [
        device(0, "Realtek Speakers [Loopback]", loopback=True, inputs=2),
        device(1, "AirPods [Loopback]", loopback=True, inputs=2),
    ]
    pa = FakePyAudio(devices, default_output=None)
    assert win.resolve_loopback_device(pa, "airpods")["name"] == "AirPods [Loopback]"
    with pytest.raises(AudioSourceError):
        win.resolve_loopback_device(pa, "Jabra")


def test_loopback_falls_back_when_no_name_matches():
    """A default endpoint with no matching loopback still records something."""
    devices = [device(0, "HDMI Output", outputs=2),
               device(1, "Speakers [Loopback]", loopback=True, inputs=2)]
    pa = FakePyAudio(devices, default_output=0)
    assert win.resolve_loopback_device(pa)["name"] == "Speakers [Loopback]"


def test_no_loopback_endpoints_is_a_clear_error():
    pa = FakePyAudio([device(0, "Speakers", outputs=2)], default_output=0)
    with pytest.raises(AudioSourceError) as excinfo:
        win.resolve_loopback_device(pa)
    assert "loopback" in str(excinfo.value)


def test_input_resolution_falls_back_to_the_portaudio_default():
    devices = [device(0, "Laptop Microphone", inputs=1)]
    pa = FakePyAudio(devices, default_input=0)
    assert win.resolve_input_device(pa)["name"] == "Laptop Microphone"


def test_missing_microphone_explains_itself():
    pa = FakePyAudio([device(0, "Speakers", outputs=2)])
    with pytest.raises(AudioSourceError) as excinfo:
        win.resolve_input_device(pa)
    assert "Windows Sound settings" in str(excinfo.value)


def test_oserror_translation_names_the_condition():
    assert "invalidated" in win._explain_oserror(OSError("[Errno -9988]"), "SYSTEM")
    assert "unavailable" in win._explain_oserror(OSError("Device unavailable"), "SYSTEM")


def test_process_loopback_is_optional_and_explains_itself(monkeypatch):
    monkeypatch.setattr(wpl, "helper_path", lambda: None)
    assert wpl.process_loopback_supported() is False
    assert "not applicable" in wpl.support_status() or "helper" in wpl.support_status()


# -- Linux discovery -----------------------------------------------------


PACTL_SINKS = (
    "0\talsa_output.pci-0000_00_1f.3.analog-stereo\tPipeWire\ts16le 2ch 48000Hz\tRUNNING\n"
    "1\tbluez_output.AC_BC_32.1\tPipeWire\ts16le 2ch 44100Hz\tSUSPENDED\n"
)
PACTL_SOURCES = (
    "0\talsa_output.pci-0000_00_1f.3.analog-stereo.monitor\tPipeWire\ts16le 2ch 48000Hz\tIDLE\n"
    "1\talsa_input.pci-0000_00_1f.3.analog-stereo\tPipeWire\ts16le 2ch 48000Hz\tRUNNING\n"
)


@pytest.fixture
def linux_backend(monkeypatch):
    def fake_run(args, timeout=3.0):
        if args[:2] == ["pactl", "info"]:
            return "Server Name: PulseAudio (on PipeWire 1.0.0)\n"
        if args[:2] == ["pactl", "get-default-sink"]:
            return "alsa_output.pci-0000_00_1f.3.analog-stereo\n"
        if args[:2] == ["pactl", "get-default-source"]:
            return "alsa_input.pci-0000_00_1f.3.analog-stereo\n"
        if args[:3] == ["pactl", "list", "short"]:
            return PACTL_SINKS if args[3] == "sinks" else PACTL_SOURCES
        return None

    monkeypatch.setattr(linux_mod, "_run", fake_run)
    monkeypatch.setattr(linux_mod, "_which",
                        lambda name: f"/usr/bin/{name}" if name in
                        ("pactl", "parec") else None)
    return linux_mod.LinuxBackend()


def test_linux_detects_pipewire_through_pactl(linux_backend):
    assert linux_backend.server == "PipeWire"
    assert linux_backend.available
    assert linux_backend.tool == "parec"
    assert "sink monitor" in linux_backend.describe_system_capture()


def test_linux_default_output_carries_its_monitor(linux_backend):
    info = linux_backend.default_output()
    assert info.name == "alsa_output.pci-0000_00_1f.3.analog-stereo"
    assert info.extra["monitor"].endswith(".monitor")
    assert info.sample_rate == 48_000


def test_linux_list_devices_includes_monitors_and_hides_them_from_inputs(linux_backend):
    devices = linux_backend.list_devices()
    kinds = {d.kind for d in devices}
    assert kinds == {"output", "loopback", "input"}
    inputs = [d for d in devices if d.kind == "input"]
    assert all(not d.name.endswith(".monitor") for d in inputs)
    assert any(d.is_default for d in inputs)


def test_linux_system_source_captures_the_active_sink_monitor(linux_backend):
    source = linux_backend.create_system_source()
    assert source.kind == "SYSTEM"
    assert source.argv[0] == "parec"
    assert "--device=alsa_output.pci-0000_00_1f.3.analog-stereo.monitor" in source.argv
    assert "--format=float32le" in source.argv


def test_linux_explicit_device_gets_a_monitor_suffix(linux_backend):
    source = linux_backend.create_system_source("bluez_output.AC_BC_32.1")
    assert "--device=bluez_output.AC_BC_32.1.monitor" in source.argv


def test_linux_pw_record_argv_when_only_pipewire_tools_exist(monkeypatch, linux_backend):
    monkeypatch.setattr(linux_backend, "has_parec", False)
    monkeypatch.setattr(linux_backend, "has_pw_record", True)
    source = linux_backend.create_microphone_source()
    assert source.argv[0] == "pw-record"
    assert "--target" in source.argv and "--format" in source.argv


def test_linux_without_an_audio_server_is_explicit_not_a_stack_trace(monkeypatch):
    monkeypatch.setattr(linux_mod, "_run", lambda *a, **k: None)
    monkeypatch.setattr(linux_mod, "_which", lambda name: None)
    backend = linux_mod.LinuxBackend()
    assert not backend.available
    reports = backend.check_permissions()
    assert all(r.status is PermissionStatus.UNSUPPORTED for r in reports)
    message = summarize(reports)
    assert "PipeWire" in message and "does not install system packages" in message
    with pytest.raises(AudioSourceError) as excinfo:
        backend.create_system_source()
    assert excinfo.value.recoverable is False


def test_linux_permissions_are_allowed_when_a_sink_exists(linux_backend):
    reports = linux_backend.check_permissions()
    assert [r.status for r in reports] == [PermissionStatus.SUPPORTED_AND_ALLOWED] * 2
    assert summarize(reports) is None


# -- registry ------------------------------------------------------------


def test_registry_selects_an_explicit_backend():
    reset_cache()
    assert isinstance(get_backend("synthetic"), SyntheticBackend)
    assert get_backend("linux").name == "Linux"


def test_registry_reports_an_unsupported_platform(monkeypatch):
    reset_cache()
    monkeypatch.setattr("platform.system", lambda: "Haiku")
    backend = get_backend("auto", refresh=True)
    assert isinstance(backend, UnsupportedBackend)
    assert all(not r.ok for r in backend.check_permissions())
    with pytest.raises(AudioSourceError):
        backend.create_microphone_source()
    reset_cache()


def test_check_permissions_uses_the_given_backend():
    reports = check_permissions(SyntheticBackend())
    assert [r.component for r in reports] == ["System audio", "Microphone"]
    assert all(r.ok for r in reports)
