#!/usr/bin/env python3
"""One-command setup for the meeting recorder.

    python3 bootstrap.py          (macOS / Linux)
    py bootstrap.py               (Windows)

Creates a local virtual environment in ``.venv``, installs the dependencies
this platform needs, builds the macOS system-audio helper when Swift is
available, and finishes by running the permission check.

Everything happens inside this folder, as an ordinary user. There is no
sudo, no UAC, no system package installation and no driver install — the
recorder cannot work that way by design.

Note the difference from spec 21: the *recorder* never installs anything
behind your back. This script is separate, you run it deliberately, and it
prints every command before running it.

Options:
    --dev          also install pytest and run the test suite
    --recreate     delete an existing .venv and start over
    --no-check     skip the closing permission check
    --python PATH  interpreter to build the environment with
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import venv

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(ROOT, ".venv")
MIN_PYTHON = (3, 9)
HELPER_SOURCE = os.path.join(ROOT, "helpers", "macos", "SystemAudioCapture.swift")
HELPER_BINARY = os.path.join(ROOT, "helpers", "macos", "meetingcap-system-audio")


# -- small helpers -------------------------------------------------------

def say(message: str = "") -> None:
    print(message, flush=True)


def step(message: str) -> None:
    say(f"\n==> {message}")


def run(argv, *, check: bool = True, cwd: str = ROOT) -> int:
    say("    $ " + " ".join(argv))
    result = subprocess.run(argv, cwd=cwd)
    if check and result.returncode != 0:
        raise SystemExit(f"\nFailed: {' '.join(argv)}")
    return result.returncode


def venv_python() -> str:
    if platform.system() == "Windows":
        return os.path.join(VENV_DIR, "Scripts", "python.exe")
    return os.path.join(VENV_DIR, "bin", "python")


def venv_command(name: str) -> str:
    if platform.system() == "Windows":
        return os.path.join(VENV_DIR, "Scripts", f"{name}.exe")
    return os.path.join(VENV_DIR, "bin", name)


def relative(path: str) -> str:
    try:
        return os.path.relpath(path, os.getcwd())
    except ValueError:  # pragma: no cover - different drive on Windows
        return path


# -- steps ---------------------------------------------------------------

def check_python() -> None:
    if sys.version_info < MIN_PYTHON:
        raise SystemExit(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is required; "
            f"this is {platform.python_version()}.")
    say(f"Python {platform.python_version()} on {platform.system()} "
        f"{platform.release()} ({platform.machine()})")


def create_venv(recreate: bool, interpreter: str) -> None:
    if recreate and os.path.isdir(VENV_DIR):
        step(f"Removing the existing environment at {relative(VENV_DIR)}")
        shutil.rmtree(VENV_DIR)
    if os.path.isdir(VENV_DIR) and os.path.exists(venv_python()):
        step(f"Using the existing environment at {relative(VENV_DIR)}")
        return
    step(f"Creating a virtual environment in {relative(VENV_DIR)}")
    if interpreter and interpreter != sys.executable:
        run([interpreter, "-m", "venv", VENV_DIR])
    else:
        venv.EnvBuilder(with_pip=True, clear=False).create(VENV_DIR)
        say(f"    created with {sys.executable} -m venv")


def install(dev: bool) -> None:
    python = venv_python()
    step("Installing dependencies (no administrator privileges required)")
    target = ".[dev]" if dev else "."
    code = run([python, "-m", "pip", "install", "--upgrade", target], check=False)
    if code != 0:
        say("\n    Full install failed. Falling back to the core dependency so")
        say("    the recorder still runs where it can.")
        run([python, "-m", "pip", "install", "--upgrade", "numpy"])
        if dev:
            run([python, "-m", "pip", "install", "--upgrade", "pytest"], check=False)
        if platform.system() == "Windows":
            say("\n    PyAudioWPatch did not install. It is what exposes WASAPI")
            say("    loopback to Python, so system-audio capture will be")
            say("    unavailable until it does. Retry with:")
            say(f"      {relative(python)} -m pip install PyAudioWPatch")


def build_macos_helper() -> None:
    if platform.system() != "Darwin":
        return
    step("Building the macOS system-audio helper (ScreenCaptureKit)")
    if not shutil.which("swiftc"):
        say("    swiftc was not found. Install the command line tools with:")
        say("      xcode-select --install")
        say("    then re-run this script. Until then the microphone still")
        say("    records; system audio does not.")
        return
    code = run(["swiftc", "-O",
                "-framework", "ScreenCaptureKit",
                "-framework", "AVFoundation",
                "-o", HELPER_BINARY, HELPER_SOURCE], check=False)
    if code == 0:
        say(f"    built {relative(HELPER_BINARY)}")
    else:
        say("    The helper did not build. See helpers/macos/README.md.")
        say("    The microphone still records without it.")


def run_tests() -> None:
    step("Running the test suite")
    run([venv_python(), "-m", "pytest", "-q"], check=False)


def run_check() -> None:
    step("Checking devices and permissions")
    console = venv_command("meeting-capture")
    argv = ([console] if os.path.exists(console)
            else [venv_python(), "-m", "meetingcap"])
    run(argv + ["--check"], check=False)


def final_instructions(dev: bool) -> None:
    python = relative(venv_python())
    command = relative(venv_command("meeting-capture"))
    say("\n" + "=" * 62)
    say("Setup complete. To record:")
    say("")
    say(f"    {command}")
    say("")
    say("Other things you can do:")
    say(f"    {command} --diagnostics      test capture before a real meeting")
    say(f"    {command} --list-devices     show what will be recorded")
    say(f"    {command} --output ./meetings --title \"ACME call\"")
    say("")
    say("Equivalent without the console script:")
    say(f"    {python} meeting_capture.py")
    if dev:
        say(f"    {python} -m pytest")
    say("")
    say("Recordings land in ./meetings/<date>_<title>/ as system.wav,")
    say("microphone.wav and meeting_mix.wav. Press Ctrl+C to stop.")
    if platform.system() == "Darwin":
        say("")
        say("macOS will ask for Screen & System Audio Recording and Microphone")
        say("permission on first run. Grant them in System Settings -> Privacy")
        say("& Security; no administrator password is involved.")
    say("=" * 62)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Set up the meeting recorder in a local virtual environment.")
    parser.add_argument("--dev", action="store_true",
                        help="install pytest and run the test suite")
    parser.add_argument("--recreate", action="store_true",
                        help="delete an existing .venv first")
    parser.add_argument("--no-check", action="store_true",
                        help="skip the closing permission check")
    parser.add_argument("--python", default=sys.executable,
                        help="interpreter to build the environment with")
    args = parser.parse_args()

    say("Zero-admin meeting recorder — setup")
    say("-" * 35)
    check_python()
    create_venv(args.recreate, args.python)
    install(args.dev)
    build_macos_helper()
    if args.dev:
        run_tests()
    if not args.no_check:
        run_check()
    final_instructions(args.dev)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:  # pragma: no cover - user abort
        say("\nInterrupted.")
        sys.exit(130)
