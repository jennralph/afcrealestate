"""``--diagnostics``: prove the capture path works before a real meeting (spec 43).

    SYSTEM LOOPBACK .... PASS
    MICROPHONE ......... PASS
    48k RESAMPLING ..... PASS
    TIMESTAMPING ....... PASS
    DEVICE WATCHER ..... PASS
    DISK WRITER ........ PASS

The audio checks are interactive by design: the user is asked to play
ordinary, non-DRM audio and then to speak, and the measured RMS is what
decides PASS.  ``--non-interactive`` skips the prompts and simply measures
whatever is already playing.
"""

from __future__ import annotations

import math
import os
import shutil
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import List, Optional, TextIO

import numpy as np

from . import MIC, SYSTEM
from .audio.backend import PlatformBackend
from .audio.base import AudioSourceError
from .audio.formats import Normalizer, Resampler, dbfs
from .audio.registry import get_backend
from .capture.session import SessionMetadata
from .capture.watcher import DeviceWatcher
from .capture.wavio import parse_wav_header, read_wav, repair_wav, WavWriter
from .deps import missing as missing_deps, report as deps_report
from .permissions import PermissionStatus

SILENCE_FLOOR_DB = -60.0
MEASURE_SECONDS = 5.0
LABEL_WIDTH = 20


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    skipped: bool = False

    def line(self) -> str:
        dots = "." * max(1, LABEL_WIDTH - len(self.name))
        verdict = "SKIP" if self.skipped else ("PASS" if self.passed else "FAIL")
        text = f"{self.name} {dots} {verdict}"
        return f"{text}   {self.detail}" if self.detail else text


class Diagnostics:
    def __init__(self, backend: Optional[PlatformBackend] = None, *,
                 interactive: bool = True, stream: Optional[TextIO] = None,
                 seconds: float = MEASURE_SECONDS,
                 output_dir: Optional[str] = None) -> None:
        self.backend = backend or get_backend()
        self.interactive = interactive
        self.stream = stream or sys.stdout
        self.seconds = seconds
        self.output_dir = output_dir or tempfile.gettempdir()
        self.results: List[CheckResult] = []

    # -- driver ----------------------------------------------------------

    def run(self) -> bool:
        self._say(f"Diagnostics — {self.backend.describe_system_capture()}")
        self._say("No administrator password is required by this application.")
        self._say("")

        self._record(self.check_dependencies())
        self._record(self.check_permissions())
        self._record(self.check_system_capture())
        self._record(self.check_microphone())
        self._record(self.check_resampling())
        self._record(self.check_timestamping())
        self._record(self.check_device_watcher())
        self._record(self.check_disk_writer())

        self._say("")
        failed = [r for r in self.results if not r.passed and not r.skipped]
        if failed:
            self._say(f"{len(failed)} check(s) failed.")
        else:
            self._say("All checks passed.")
        return not failed

    def _record(self, result: CheckResult) -> CheckResult:
        self.results.append(result)
        self._say(result.line())
        return result

    def _say(self, text: str) -> None:
        self.stream.write(text + "\n")
        self.stream.flush()

    def _ask(self, prompt: str) -> None:
        if not self.interactive:
            return
        self.stream.write(prompt + " (press ENTER) ")
        self.stream.flush()
        try:
            sys.stdin.readline()
        except (KeyboardInterrupt, EOFError):  # pragma: no cover - user abort
            raise

    # -- checks ----------------------------------------------------------

    def check_dependencies(self) -> CheckResult:
        gaps = missing_deps()
        if gaps:
            return CheckResult("DEPENDENCIES", False, deps_report().replace("\n", " | "))
        return CheckResult("DEPENDENCIES", True)

    def check_permissions(self) -> CheckResult:
        reports = self.backend.check_permissions()
        blocked = [r for r in reports if not r.ok]
        if not blocked:
            return CheckResult("PERMISSIONS", True)
        detail = "; ".join(f"{r.component}: {r.status.value}" for r in blocked)
        for report in blocked:
            if report.status is PermissionStatus.SUPPORTED_BUT_PERMISSION_MISSING:
                self._say("")
                self._say(report.render())
                self._say("")
        return CheckResult("PERMISSIONS", False, detail)

    def check_system_capture(self) -> CheckResult:
        self._ask("Play ordinary, non-DRM audio (music or a video) now, then")
        return self._measure(SYSTEM, "SYSTEM LOOPBACK")

    def check_microphone(self) -> CheckResult:
        self._ask("Speak normally for a few seconds after you")
        return self._measure(MIC, "MICROPHONE")

    def _measure(self, kind: str, label: str) -> CheckResult:
        try:
            source = (self.backend.create_system_source() if kind == SYSTEM
                      else self.backend.create_microphone_source())
        except AudioSourceError as exc:
            return CheckResult(label, False, str(exc))
        normalizer = Normalizer(kind, max_channels=2 if kind == SYSTEM else 1)
        levels: List[float] = []
        timestamps: List[int] = []
        frames = 0
        try:
            source.start()
        except AudioSourceError as exc:
            return CheckResult(label, False, str(exc))
        try:
            deadline = time.monotonic() + self.seconds
            while time.monotonic() < deadline:
                chunk = source.read(timeout=0.5)
                if chunk is None:
                    continue
                block = normalizer.process(chunk)
                if block.pcm.size == 0:
                    continue
                levels.append(dbfs(block.pcm))
                timestamps.append(chunk.timestamp_monotonic)
                frames += block.frame_count
        finally:
            source.stop()

        # Kept for the timestamping check so it measures real capture.
        setattr(self, f"_{kind.lower()}_timestamps", timestamps)
        setattr(self, f"_{kind.lower()}_frames", frames)

        if not levels:
            return CheckResult(label, False, "no audio blocks were delivered")
        peak = max(levels)
        detail = f"peak {peak:.1f} dBFS over {frames / 48000:.1f}s"
        if peak <= SILENCE_FLOOR_DB:
            hint = ("nothing was playing?" if kind == SYSTEM else "microphone muted?")
            return CheckResult(label, False, f"{detail} — signal is silent ({hint})")
        return CheckResult(label, True, detail)

    def check_resampling(self) -> CheckResult:
        """44.1 kHz in, 48 kHz out: right length, right frequency, no seam."""
        src_rate, dst_rate, freq = 44_100, 48_000, 440.0
        resampler = Resampler(src_rate, dst_rate, 1)
        block = 441
        out_blocks = []
        phase = 0.0
        step = 2 * math.pi * freq / src_rate
        for _ in range(100):                       # one second of audio
            idx = np.arange(block, dtype=np.float32)
            wave = np.sin(phase + step * idx).astype(np.float32).reshape(-1, 1)
            phase = (phase + step * block) % (2 * math.pi)
            out_blocks.append(resampler.process(wave))
        out = np.concatenate(out_blocks, axis=0) if out_blocks else np.zeros((0, 1))
        produced = out.shape[0]
        expected = dst_rate
        if abs(produced - expected) > dst_rate * 0.01:
            return CheckResult("48k RESAMPLING", False,
                               f"produced {produced} frames, expected ~{expected}")
        # Frequency check via zero crossings, tolerant of interpolation error.
        signal = out[:, 0]
        crossings = int(np.sum((signal[:-1] < 0) & (signal[1:] >= 0)))
        measured = crossings / (produced / dst_rate)
        if abs(measured - freq) > 5.0:
            return CheckResult("48k RESAMPLING", False,
                               f"tone measured at {measured:.0f} Hz, expected {freq:.0f} Hz")
        return CheckResult("48k RESAMPLING", True,
                           f"{produced} frames, {measured:.0f} Hz")

    def check_timestamping(self) -> CheckResult:
        stamps = list(getattr(self, "_system_timestamps", [])) or \
            list(getattr(self, "_mic_timestamps", []))
        if len(stamps) < 3:
            return CheckResult("TIMESTAMPING", False, "not enough captured blocks")
        deltas = [b - a for a, b in zip(stamps, stamps[1:])]
        if any(d <= 0 for d in deltas):
            return CheckResult("TIMESTAMPING", False, "monotonic clock went backwards")
        jitter_ms = statistics.pstdev(deltas) / 1e6 if len(deltas) > 1 else 0.0
        span_s = (stamps[-1] - stamps[0]) / 1e9
        return CheckResult("TIMESTAMPING", True,
                           f"{len(stamps)} blocks over {span_s:.1f}s, "
                           f"jitter {jitter_ms:.1f} ms")

    def check_device_watcher(self) -> CheckResult:
        seen: List[str] = []
        watcher = DeviceWatcher(self.backend, lambda change: seen.append(change.type),
                                interval=0.2)
        watcher.prime()
        watcher.poll()
        output = self.backend.default_output()
        source = self.backend.default_input()
        if output is None and source is None:
            return CheckResult("DEVICE WATCHER", False,
                               "no default endpoints could be enumerated")
        detail = " / ".join(filter(None, [
            f"out: {output.name}" if output else "",
            f"in: {source.name}" if source else "",
        ]))
        return CheckResult("DEVICE WATCHER", True, detail)

    def check_disk_writer(self) -> CheckResult:
        """Write, rotate, kill, repair — the crash path from spec 16."""
        temp_dir = tempfile.mkdtemp(prefix="meetingcap-diag-", dir=self.output_dir)
        try:
            session = SessionMetadata(temp_dir)
            path = os.path.join(temp_dir, "probe.wav")
            tone = np.sin(np.linspace(0, 2 * math.pi * 440, 4800, dtype=np.float32))
            writer = WavWriter(path, 48_000, 1, fmt="int16")
            writer.write(tone.reshape(-1, 1))
            writer.close()
            pcm, rate = read_wav(path)
            if rate != 48_000 or pcm.shape[0] != 4800:
                return CheckResult("DISK WRITER", False,
                                   f"read back {pcm.shape[0]} frames @ {rate} Hz")

            # Simulate a process death: the data is on disk but the header
            # sizes were never patched, which is exactly what a killed
            # recorder leaves behind.
            torn = os.path.join(temp_dir, "torn.wav")
            shutil.copyfile(path, torn)
            _blank_riff_sizes(torn)
            info = repair_wav(torn)
            if info is None or info.frame_count != 4800:
                return CheckResult("DISK WRITER", False,
                                   "an unfinalised segment could not be repaired")
            free_mb = shutil.disk_usage(self.output_dir).free / (1 << 20)
            if not os.path.exists(session.path):
                return CheckResult("DISK WRITER", False, "session.json was not written")
            return CheckResult("DISK WRITER", True,
                               f"segment + repair OK, {free_mb:.0f} MB free")
        except OSError as exc:
            return CheckResult("DISK WRITER", False, str(exc))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _blank_riff_sizes(path: str) -> None:
    """Zero the RIFF and data sizes, as an unfinalised segment has them."""
    import struct

    info = parse_wav_header(path)
    if info is None:  # pragma: no cover - we just wrote this file
        return
    with open(path, "r+b") as fh:
        fh.seek(4)
        fh.write(struct.pack("<I", 0))
        fh.seek(info.data_offset - 4)
        fh.write(struct.pack("<I", 0))


def run_diagnostics(backend: Optional[PlatformBackend] = None, *,
                    interactive: bool = True, seconds: float = MEASURE_SECONDS,
                    stream: Optional[TextIO] = None) -> bool:
    return Diagnostics(backend, interactive=interactive, seconds=seconds,
                       stream=stream).run()
