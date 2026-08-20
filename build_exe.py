#!/usr/bin/env python3
"""Build a single-file executable of the recorder.

    python build_exe.py

Run it on the platform you are building for — PyInstaller does not
cross-compile, so a Windows .exe has to be built on Windows.

Output:
    Windows   dist\\MeetingCapture.exe     ~40 MB, double-clickable
    macOS     dist/MeetingCapture
    Linux     dist/MeetingCapture

If you have no Windows machine, push the branch instead: the
`.github/workflows/build-installers.yml` workflow builds the same executable
on a GitHub-hosted Windows runner and uploads it as an artifact.

Options:
    --venv       build inside ./.venv (created by setup.sh / setup.ps1)
    --clean      remove build/ and dist/ first
    --no-test    skip the smoke test of the finished executable
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SPEC = os.path.join(ROOT, "packaging", "MeetingCapture.spec")
DIST = os.path.join(ROOT, "dist")
BUILD = os.path.join(ROOT, "build")


def say(message: str = "") -> None:
    print(message, flush=True)


def step(message: str) -> None:
    say(f"\n==> {message}")


def run(argv, *, check: bool = True) -> int:
    say("    $ " + " ".join(argv))
    result = subprocess.run(argv, cwd=ROOT)
    if check and result.returncode != 0:
        raise SystemExit(f"\nFailed: {' '.join(argv)}")
    return result.returncode


def venv_python() -> str:
    if platform.system() == "Windows":
        return os.path.join(ROOT, ".venv", "Scripts", "python.exe")
    return os.path.join(ROOT, ".venv", "bin", "python")


def executable_path() -> str:
    name = "MeetingCapture.exe" if platform.system() == "Windows" else "MeetingCapture"
    return os.path.join(DIST, name)


def ensure_pyinstaller(python: str) -> None:
    step("Checking for PyInstaller")
    probe = subprocess.run([python, "-c", "import PyInstaller"],
                           capture_output=True)
    if probe.returncode == 0:
        say("    already installed")
        return
    say("    not installed — installing it now (no administrator rights needed)")
    run([python, "-m", "pip", "install", "--upgrade", "pyinstaller"])


def build_macos_helper() -> None:
    """Bundle system-audio capture rather than shipping a half-working app."""
    if platform.system() != "Darwin":
        return
    helper = os.path.join(ROOT, "helpers", "macos", "meetingcap-system-audio")
    if os.path.exists(helper):
        return
    step("Building the macOS system-audio helper so it can be bundled")
    if not shutil.which("swiftc"):
        say("    swiftc not found (xcode-select --install). The app will be")
        say("    built without it: the microphone records, system audio does not.")
        return
    run(["swiftc", "-O", "-framework", "ScreenCaptureKit",
         "-framework", "AVFoundation", "-o", helper,
         os.path.join(ROOT, "helpers", "macos", "SystemAudioCapture.swift")],
        check=False)


def smoke_test() -> None:
    """A build that cannot answer --check is not worth sending to anyone."""
    step("Smoke-testing the executable")
    path = executable_path()
    for args in (["--version"], ["--list-devices", "--source", "synthetic"]):
        result = subprocess.run([path] + args, capture_output=True, text=True,
                                timeout=180)
        say(f"    $ {os.path.basename(path)} {' '.join(args)}  ->  "
            f"exit {result.returncode}")
        if result.returncode != 0:
            say(result.stdout)
            say(result.stderr)
            raise SystemExit("The built executable does not run.")
    result = subprocess.run(
        [path, "--source", "synthetic", "--duration", "2", "--non-interactive",
         "--output", os.path.join(BUILD, "smoke-meetings")],
        capture_output=True, text=True, timeout=300)
    if result.returncode != 0 or "Recording finished." not in result.stdout:
        say(result.stdout)
        say(result.stderr)
        raise SystemExit("The built executable could not complete a recording.")
    say("    recorded a synthetic session end to end")


def report() -> None:
    path = executable_path()
    size_mb = os.path.getsize(path) / (1 << 20)
    say("\n" + "=" * 66)
    say(f"Built {path}  ({size_mb:.0f} MB)")
    say("")
    say("Send that single file to anyone. They double-click it; it needs no")
    say("Python, no installer and no administrator rights.")
    if platform.system() == "Windows":
        say("")
        say("First run on someone else's PC: Windows SmartScreen will warn that")
        say("the publisher is unknown, because the file is not code-signed.")
        say("They click 'More info' -> 'Run anyway'. To remove that warning you")
        say("need an Authenticode code-signing certificate.")
    if platform.system() == "Darwin":
        say("")
        say("macOS Gatekeeper will block an unsigned build downloaded from the")
        say("internet. Either sign and notarise it, or the recipient must use")
        say("right-click -> Open the first time.")
    say("=" * 66)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a single-file executable.")
    parser.add_argument("--venv", action="store_true",
                        help="build using ./.venv instead of the current Python")
    parser.add_argument("--clean", action="store_true",
                        help="remove build/ and dist/ first")
    parser.add_argument("--no-test", action="store_true",
                        help="skip the smoke test of the finished executable")
    args = parser.parse_args()

    python = venv_python() if args.venv else sys.executable
    if args.venv and not os.path.exists(python):
        raise SystemExit("No ./.venv found. Run setup.sh (or setup.ps1) first, "
                         "or drop --venv.")

    say("Zero-admin meeting recorder — executable build")
    say("-" * 46)
    say(f"{platform.system()} {platform.release()} ({platform.machine()}), "
        f"Python {platform.python_version()}")
    if platform.system() != "Windows":
        say("\nNote: this produces a native binary for THIS platform. A Windows")
        say(".exe must be built on Windows — PyInstaller does not cross-compile.")

    if args.clean:
        step("Cleaning previous build output")
        for path in (BUILD, DIST):
            shutil.rmtree(path, ignore_errors=True)
            say(f"    removed {os.path.relpath(path, ROOT)}")

    ensure_pyinstaller(python)
    build_macos_helper()

    step("Running PyInstaller")
    run([python, "-m", "PyInstaller", "--clean", "--noconfirm", SPEC])

    if not os.path.exists(executable_path()):
        raise SystemExit(f"Expected {executable_path()} but it was not produced.")
    if not args.no_test:
        smoke_test()
    report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
