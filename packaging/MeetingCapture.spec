# PyInstaller spec for the zero-admin meeting recorder.
#
#   python build_exe.py            (from the repository root)
#   pyinstaller --clean --noconfirm packaging/MeetingCapture.spec
#
# Produces a single self-contained executable:
#   Windows  dist/MeetingCapture.exe
#   macOS    dist/MeetingCapture
#   Linux    dist/MeetingCapture
#
# PyInstaller does not cross-compile: build on the platform you are targeting.
#
# The build bundles an interpreter and the Python dependencies. It bundles no
# audio driver and no virtual audio device, and the packaged program still
# requests no elevation — it uses the same WASAPI / ScreenCaptureKit /
# PipeWire paths as a source checkout.

import os
import sys

from PyInstaller.utils.hooks import collect_dynamic_libs

ROOT = os.path.dirname(os.path.abspath(SPECPATH))          # noqa: F821 - PyInstaller global
ENTRY = os.path.join(ROOT, "meeting_capture.py")

binaries = []
datas = []

# sounddevice ships PortAudio as a data file rather than an extension module,
# so it needs collecting explicitly (macOS microphone capture).
if sys.platform == "darwin":
    try:
        binaries += collect_dynamic_libs("sounddevice")
        datas += [(os.path.join(os.path.dirname(__import__("sounddevice").__file__),
                                "_sounddevice_data"), "_sounddevice_data")]
    except Exception:
        pass

    # Bundle the ScreenCaptureKit helper when it has been built, so the
    # packaged app can capture system audio on a machine with no Xcode.
    helper = os.path.join(ROOT, "helpers", "macos", "meetingcap-system-audio")
    if os.path.exists(helper):
        binaries += [(helper, "helpers/macos")]

# Windows process loopback is optional (spec 7); bundle the helper if present.
if sys.platform == "win32":
    helper = os.path.join(ROOT, "helpers", "windows",
                          "meetingcap-process-loopback.exe")
    if os.path.exists(helper):
        binaries += [(helper, "helpers/windows")]

hiddenimports = [
    "meetingcap",
    "meetingcap.cli",
    "meetingcap.deps",
    "meetingcap.diagnostics",
    "meetingcap.frozen",
    "meetingcap.permissions",
    "meetingcap.audio.backend",
    "meetingcap.audio.base",
    "meetingcap.audio.formats",
    "meetingcap.audio.linux",
    "meetingcap.audio.macos",
    "meetingcap.audio.registry",
    "meetingcap.audio.synthetic",
    "meetingcap.audio.windows",
    "meetingcap.audio.windows_process_loopback",
    "meetingcap.capture.mixer",
    "meetingcap.capture.pipeline",
    "meetingcap.capture.recorder",
    "meetingcap.capture.session",
    "meetingcap.capture.sync",
    "meetingcap.capture.watcher",
    "meetingcap.capture.watchdog",
    "meetingcap.capture.wavio",
    "meetingcap.capture.writer",
    "meetingcap.ui.console",
]
# Platform adapters are selected by name at runtime, so name the optional
# third-party ones here too; a missing one is dropped by `excludes` below.
if sys.platform == "win32":
    hiddenimports += ["pyaudiowpatch"]
if sys.platform == "darwin":
    hiddenimports += ["sounddevice"]

# Nothing in this program draws a GUI or plots anything: keeping these out
# takes tens of megabytes off the download.
excludes = [
    "tkinter", "matplotlib", "PIL", "PyQt5", "PyQt6", "PySide2", "PySide6",
    "IPython", "jupyter", "pytest", "setuptools", "pandas", "scipy",
]
if sys.platform != "win32":
    excludes += ["pyaudiowpatch"]
if sys.platform != "darwin":
    excludes += ["sounddevice", "ScreenCaptureKit", "AVFoundation", "Quartz"]

analysis = Analysis(                                        # noqa: F821
    [ENTRY],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(analysis.pure)                                    # noqa: F821

exe = EXE(                                                  # noqa: F821
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="MeetingCapture",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX-packed binaries trip antivirus heuristics
    runtime_tmpdir=None,
    console=True,            # the recorder *is* a console UI (spec 19)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=os.environ.get("MEETINGCAP_CODESIGN_IDENTITY") or None,
    entitlements_file=os.environ.get("MEETINGCAP_ENTITLEMENTS") or None,
)
