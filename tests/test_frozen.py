"""Packaged-build behaviour (double-click, output location).

None of this changes what gets recorded; it is about a build someone can be
sent and click on without a terminal.
"""

import os

from meetingcap import cli, frozen


def test_not_frozen_in_a_source_checkout():
    assert frozen.is_frozen() is False
    assert frozen.default_output_dir() == "./meetings"


def test_frozen_build_writes_somewhere_predictable(monkeypatch, tmp_path):
    """A double-clicked exe must not scatter recordings into Downloads."""
    monkeypatch.setattr(frozen.sys, "frozen", True, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(os.path, "expanduser", lambda path: str(tmp_path))

    assert frozen.default_output_dir() == os.path.join(str(tmp_path), "MeetingCapture")

    documents = tmp_path / "Documents"
    documents.mkdir()
    assert frozen.default_output_dir() == os.path.join(str(documents), "MeetingCapture")


def test_console_is_never_held_outside_a_double_clicked_window(capsys):
    """Holding the window inside a terminal or a test would just hang."""
    assert frozen.owns_console() is False
    frozen.hold_console()                    # returns immediately
    assert capsys.readouterr().out == ""


def test_cli_uses_the_frozen_default_when_output_is_omitted(monkeypatch, tmp_path):
    captured = {}

    def fake_record(args, backend, reports):
        captured["output"] = args.output
        return 0

    monkeypatch.setattr(cli, "default_output_dir", lambda: str(tmp_path / "packaged"))
    monkeypatch.setattr(cli, "_record", fake_record)
    assert cli.main(["--source", "synthetic", "--non-interactive"]) == 0
    assert captured["output"] == str(tmp_path / "packaged")


def test_explicit_output_still_wins(monkeypatch, tmp_path):
    captured = {}

    def fake_record(args, backend, reports):
        captured["output"] = args.output
        return 0

    monkeypatch.setattr(cli, "default_output_dir", lambda: "/should/not/be/used")
    monkeypatch.setattr(cli, "_record", fake_record)
    assert cli.main(["--source", "synthetic", "--non-interactive",
                     "--output", str(tmp_path / "chosen")]) == 0
    assert captured["output"] == str(tmp_path / "chosen")


def test_bundled_path_is_none_when_not_bundled():
    assert frozen.bundled_path("helpers", "macos", "meetingcap-system-audio") is None


def test_dependency_hint_names_a_real_interpreter_when_frozen(monkeypatch):
    """sys.executable is the app itself in a bundle, not something pip runs."""
    from meetingcap import deps

    monkeypatch.setattr(deps.sys, "frozen", True, raising=False)
    command = deps.install_command(
        [deps.Dependency("numpy", "numpy", "buffers")], "Linux")
    assert command.startswith("python3 -m pip install")
