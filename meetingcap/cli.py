"""Command line interface (spec 18).

    python meeting_capture.py
    python meeting_capture.py --list-devices
    python meeting_capture.py --output ./meetings
    python meeting_capture.py --mic auto
    python meeting_capture.py --system auto
    python meeting_capture.py --no-mix
    python meeting_capture.py --diagnostics

Defaults: microphone = the current/default communication microphone,
system = the operating system's audio.  Running it with no arguments is
enough.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from typing import List, Optional

from . import __version__
from .audio.base import AudioSourceError
from .audio.registry import get_backend
from .capture.recorder import Recorder, RecorderConfig
from .capture.wavio import repair_wav
from .deps import missing as missing_deps, report as deps_report, setup_instructions
from .diagnostics import run_diagnostics
from .permissions import blocking as blocking_permissions
from .ui import console

REFRESH_INTERVAL = 0.2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meeting_capture.py",
        description="Zero-admin meeting recorder: captures system audio and "
                    "microphone as independent tracks. Never requires "
                    "administrator/root privileges and never bypasses an "
                    "operating-system privacy permission.")
    parser.add_argument("--list-devices", action="store_true",
                        help="list audio devices and exit")
    parser.add_argument("--output", default="./meetings", metavar="DIR",
                        help="where to store sessions (default: ./meetings)")
    parser.add_argument("--mic", default="auto", metavar="NAME",
                        help="microphone to capture (default: auto)")
    parser.add_argument("--system", default="auto", metavar="NAME",
                        help="system audio endpoint to capture (default: auto)")
    parser.add_argument("--no-mix", action="store_true",
                        help="do not write meeting_mix.wav")
    parser.add_argument("--no-system", action="store_true",
                        help="capture the microphone only")
    parser.add_argument("--no-microphone", action="store_true",
                        help="capture system audio only")
    parser.add_argument("--diagnostics", action="store_true",
                        help="run the capture self-test and exit")
    parser.add_argument("--check", action="store_true",
                        help="report permissions and dependencies, then exit")
    parser.add_argument("--title", default=None,
                        help="name for the session directory")
    parser.add_argument("--duration", type=float, default=None, metavar="SECONDS",
                        help="stop automatically after this many seconds")
    parser.add_argument("--source", default="auto",
                        choices=["auto", "synthetic", "windows", "macos", "linux"],
                        help="force a capture backend (synthetic needs no hardware)")
    parser.add_argument("--wav-format", default="int16", choices=["int16", "float32"],
                        help="sample format written to disk (default: int16)")
    parser.add_argument("--rotate-seconds", type=float, default=300.0,
                        help="segment rotation interval (default: 300)")
    parser.add_argument("--non-interactive", action="store_true",
                        help="never wait for ENTER (diagnostics and startup)")
    parser.add_argument("--repair", metavar="DIR",
                        help="repair the WAV segments of an interrupted session "
                             "and exit")
    parser.add_argument("--version", action="version", version=f"meetingcap {__version__}")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    gaps = missing_deps()
    if gaps:
        # Spec 21: never install anything silently; print one copyable command.
        print(deps_report())
        print()
        print("Suggested setup:")
        print(setup_instructions())
        return 2

    if args.repair:
        return _repair(args.repair)

    backend = get_backend(args.source)

    if args.list_devices:
        print(console.render_devices(backend.list_devices(),
                                     f"{backend.describe_system_capture()} devices"))
        return 0

    if args.diagnostics:
        ok = run_diagnostics(backend, interactive=not args.non_interactive)
        return 0 if ok else 1

    reports = backend.check_permissions()
    if args.check:
        print(console.render_banner(backend.describe_system_capture(), reports,
                                    _device_name(backend.default_output()),
                                    _device_name(backend.default_input())))
        return 0 if not blocking_permissions(reports) else 1

    return _record(args, backend, reports)


def _record(args, backend, reports) -> int:
    config = RecorderConfig(
        output_dir=args.output,
        title=args.title,
        backend_name=args.source,
        system_device=args.system,
        mic_device=args.mic,
        capture_system=not args.no_system,
        capture_microphone=not args.no_microphone,
        mix=not args.no_mix,
        wav_format=args.wav_format,
        rotate_seconds=args.rotate_seconds,
    )
    recorder = Recorder(config, backend=backend)

    print(console.render_banner(backend.describe_system_capture(), reports,
                                _device_name(backend.default_output()),
                                _device_name(backend.default_input()),
                                os.path.abspath(args.output)))
    blocked = blocking_permissions(reports)
    if blocked and len(blocked) == len(reports):
        # Everything is blocked: explain, do not attempt to work around it.
        print()
        print("Nothing can be captured until the above is resolved.")
        return 1

    if not args.non_interactive and sys.stdin.isatty():
        print()
        try:
            input("Press ENTER to begin. ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 130

    try:
        directory = recorder.start()
    except AudioSourceError as exc:
        print(f"\nCould not start recording: {exc}")
        return 1
    print(f"\nRecording to {directory}\n")

    stop_requested = {"value": False}

    def handle_stop(signum, frame):        # noqa: ARG001 - signal signature
        stop_requested["value"] = True

    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, handle_stop)

    view = console.LiveView()
    deadline = (time.monotonic() + args.duration) if args.duration else None
    try:
        while not stop_requested["value"]:
            view.update(recorder.status())
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(REFRESH_INTERVAL)
    finally:
        signal.signal(signal.SIGINT, previous)
        view.finish()

    result = recorder.stop()
    print(console.render_summary(result))
    return 0


def _repair(directory: str) -> int:
    """Finalise the headers of segments left behind by a killed process."""
    audio_dir = os.path.join(directory, "audio")
    search = audio_dir if os.path.isdir(audio_dir) else directory
    if not os.path.isdir(search):
        print(f"No such session directory: {directory}")
        return 1
    repaired = 0
    for name in sorted(os.listdir(search)):
        if not name.lower().endswith(".wav"):
            continue
        path = os.path.join(search, name)
        info = repair_wav(path)
        if info is None:
            print(f"  {name}: unreadable header, skipped")
            continue
        repaired += 1
        print(f"  {name}: {info.frame_count} frames ({info.duration_s:.1f}s)")
    print(f"Repaired {repaired} segment(s) in {search}")
    return 0 if repaired else 1


def _device_name(info) -> str:
    return info.name if info is not None else ""
