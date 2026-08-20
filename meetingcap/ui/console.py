"""Console output (spec 19).

Recording state is always obvious: the banner states what will be captured
and whether elevation is required (it is not), and the live view shows the
meters, the devices in use and every interruption as it happens.
"""

from __future__ import annotations

import sys
from typing import List, Optional, TextIO

from ..capture.recorder import RecorderStatus
from ..permissions import PermissionReport, PermissionStatus

OK = "OK"
BAR_WIDTH = 10


def _supports_ansi(stream: TextIO) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)())


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def render_banner(system_capture: str, reports: List[PermissionReport],
                  output_device: str = "", input_device: str = "",
                  output_dir: str = "") -> str:
    """The startup block from spec 19."""
    lines = ["Meeting Capture", "-" * 15]
    status_by_component = {r.component: r for r in reports}

    system = status_by_component.get("System audio")
    if system is not None:
        lines.append(f"System audio: {system_capture}"
                     f"{'  ' + OK if system.ok else '  ' + system.status.value}")
    if output_device:
        out_ok = OK if (system is not None and system.ok) else ""
        lines.append(f"Output: {output_device}  {out_ok}".rstrip())

    mic = status_by_component.get("Microphone")
    if mic is not None:
        label = input_device or "default microphone"
        lines.append(f"Microphone: {label}"
                     f"{'  ' + OK if mic.ok else '  ' + mic.status.value}")

    lines.append(f"Administrator privileges: NOT REQUIRED  {OK}")
    if output_dir:
        lines.append(f"Recording to: {output_dir}")

    problems = [r for r in reports if not r.ok]
    if problems:
        lines.append("")
        for report in problems:
            lines.append(report.render())
            lines.append("")
    return "\n".join(lines).rstrip()


def render_status(status: RecorderStatus) -> List[str]:
    """The live recording view from spec 19."""
    lines = [
        f"* RECORDING {format_duration(status.elapsed_s)}",
        f"  SYSTEM {status.system_bar}  {status.system_db:6.1f} dB",
        f"  MIC    {status.mic_bar}  {status.mic_db:6.1f} dB",
        f"  System device: {status.system_device or '-'}",
        f"  Mic device:    {status.mic_device or '-'}",
        f"  Dropouts: {status.dropouts}   Device switches: {status.device_switches}"
        f"   Overruns: {status.queue_overruns}",
    ]
    for warning in status.warnings[-3:]:
        lines.append(f"  ! {warning}")
    lines.append("  Press Ctrl+C to stop.")
    return lines


class LiveView:
    """Repaints the status block in place on a terminal, appends elsewhere."""

    def __init__(self, stream: Optional[TextIO] = None) -> None:
        self.stream = stream or sys.stdout
        self.ansi = _supports_ansi(self.stream)
        self._painted = 0

    def update(self, status: RecorderStatus) -> None:
        lines = render_status(status)
        if self.ansi:
            if self._painted:
                self.stream.write(f"\033[{self._painted}A")
            for line in lines:
                self.stream.write("\033[2K" + line + "\n")
            self._painted = len(lines)
        else:
            self.stream.write(lines[0] + "\n")
        self.stream.flush()

    def finish(self) -> None:
        if self.ansi and self._painted:
            self.stream.write("\n")
        self._painted = 0
        self.stream.flush()


def render_permission_problem(report: PermissionReport) -> str:
    """Spec 20's explanatory message — never a stack trace."""
    return report.render()


def render_devices(devices, title: str = "Devices") -> str:
    if not devices:
        return f"{title}: none found"
    lines = [title, "-" * len(title)]
    for kind in ("loopback", "output", "input"):
        group = [d for d in devices if d.kind == kind]
        if not group:
            continue
        lines.append(f"{kind.upper()}:")
        lines.extend(d.line() for d in group)
    lines.append("")
    lines.append("(* marks the current default; names are resolved at record "
                 "time, never hard-coded.)")
    return "\n".join(lines)


def render_summary(result) -> str:
    lines = [
        "",
        "Recording finished.",
        f"  Session:  {result.directory}",
        f"  Duration: {format_duration(result.duration_s)}",
    ]
    if result.system_wav:
        lines.append(f"  System:   {result.system_wav}")
    if result.microphone_wav:
        lines.append(f"  Mic:      {result.microphone_wav}")
    if result.mix_wav:
        lines.append(f"  Mix:      {result.mix_wav}")
    lines.append(f"  Dropouts: {result.dropouts}   "
                 f"Device switches: {result.device_switches}")
    for source, drift in sorted(result.drift.items()):
        lines.append(f"  Drift {source:<6} {drift.get('drift_seconds', 0.0):+.3f}s "
                     f"({drift.get('drift_ppm', 0.0):+.0f} ppm)")
    lines.append("  Segments are kept in audio/ — a crash costs at most the "
                 "current buffer.")
    return "\n".join(lines)


def status_word(status: PermissionStatus) -> str:
    return OK if status.ok else status.value
