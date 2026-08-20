"""The recorder: one logical meeting, two independent tracks (spec 3-22).

Responsibilities kept here — and nowhere else — are session lifecycle,
device-change recovery and finalisation.  Platform detail lives in
``meetingcap.audio``; disk detail in ``writer``/``wavio``; supervision in
``watchdog``.

The invariant the whole design serves: a device change, a stalled driver or
an invalidated endpoint interrupts a *stream*, never the *meeting*.  The
session directory, the timeline and ``session.json`` all survive it.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from .. import MIC, SYSTEM
from ..audio.backend import PlatformBackend
from ..audio.base import AudioSource, AudioSourceError
from ..audio.registry import get_backend
from ..permissions import PermissionReport, PermissionStatus
from .mixer import mix_tracks
from .pipeline import ChunkSubscriber, TrackPipeline
from .session import SessionMetadata
from .sync import SyncState
from .watcher import Backoff, DeviceChange, DeviceWatcher
from .watchdog import Watchdog
from .writer import concat_segments, TrackWriter

REOPEN_DEADLINE_S = 2.0     # spec 6 target interruption


@dataclass
class RecorderConfig:
    output_dir: str = "./meetings"
    title: Optional[str] = None
    backend_name: str = "auto"
    system_device: Optional[str] = "auto"
    mic_device: Optional[str] = "auto"
    capture_system: bool = True
    capture_microphone: bool = True
    mix: bool = True
    wav_format: str = "int16"
    rotate_seconds: float = 300.0
    watcher_interval: float = 1.0
    watchdog_interval: float = 1.0
    stall_timeout: float = 5.0
    system_max_channels: int = 2


@dataclass
class TrackState:
    name: str
    source: Optional[AudioSource] = None
    writer: Optional[TrackWriter] = None
    pipeline: Optional[TrackPipeline] = None
    device: str = ""
    enabled: bool = True
    failure: Optional[str] = None
    reopen_lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class RecorderStatus:
    elapsed_s: float = 0.0
    recording: bool = False
    system_bar: str = ".........."
    mic_bar: str = ".........."
    system_db: float = -120.0
    mic_db: float = -120.0
    system_device: str = ""
    mic_device: str = ""
    dropouts: int = 0
    device_switches: int = 0
    queue_overruns: int = 0
    warnings: List[str] = field(default_factory=list)


@dataclass
class SessionResult:
    directory: str
    session_id: str
    duration_s: float
    system_wav: Optional[str] = None
    microphone_wav: Optional[str] = None
    mix_wav: Optional[str] = None
    dropouts: int = 0
    device_switches: int = 0
    drift: Dict[str, Dict[str, float]] = field(default_factory=dict)


class Recorder:
    """Captures SYSTEM and MICROPHONE into one crash-resistant session."""

    def __init__(self, config: RecorderConfig, backend: Optional[PlatformBackend] = None) -> None:
        self.config = config
        self.backend = backend or get_backend(config.backend_name)
        self.session: Optional[SessionMetadata] = None
        self.directory = ""
        self.audio_dir = ""
        self.sync = SyncState()
        self.tracks: Dict[str, TrackState] = {
            SYSTEM: TrackState(SYSTEM, enabled=config.capture_system),
            MIC: TrackState(MIC, enabled=config.capture_microphone),
        }
        self.watchdog: Optional[Watchdog] = None
        self.watcher: Optional[DeviceWatcher] = None
        self._subscribers: List[ChunkSubscriber] = []
        self._running = False
        self._stopping = threading.Event()
        self._warnings: List[str] = []
        self._reopen_threads: List[threading.Thread] = []

    # -- preflight -------------------------------------------------------

    def preflight(self) -> List[PermissionReport]:
        """Permission and device state, for the startup banner (spec 19, 20)."""
        return self.backend.check_permissions()

    def subscribe(self, callback: ChunkSubscriber) -> None:
        """Register an ``on_audio_chunk(source, pcm, timestamp)`` consumer.

        This is the boundary transcription plugs into (spec 22).  The
        recorder itself never calls a speech-to-text backend.
        """
        self._subscribers.append(callback)
        for track in self.tracks.values():
            if track.pipeline is not None:
                track.pipeline.subscribe(callback)

    # -- lifecycle -------------------------------------------------------

    def start(self) -> str:
        """Open both tracks and begin writing.  Returns the session directory."""
        if self._running:
            return self.directory
        self.directory = self._make_session_dir()
        self.audio_dir = os.path.join(self.directory, "audio")
        os.makedirs(self.audio_dir, exist_ok=True)
        self.session = SessionMetadata(self.directory, title=self.config.title)
        self.session.set("backend", self.backend.name)
        self.session.set("system_capture", self.backend.describe_system_capture())

        self.watchdog = Watchdog(self.directory, interval=self.config.watchdog_interval,
                                 stall_timeout=self.config.stall_timeout,
                                 on_outage=self._on_outage,
                                 on_recovery=self._on_recovery)

        opened = 0
        for name in (SYSTEM, MIC):
            track = self.tracks[name]
            if not track.enabled:
                continue
            if self._open_track(track):
                opened += 1

        if opened == 0:
            self.session.set("failed", True)
            raise AudioSourceError(
                "no audio track could be opened; see the messages above",
                recoverable=False)

        self.watchdog.start()
        self.watcher = DeviceWatcher(
            self.backend, self._on_device_change,
            interval=self.config.watcher_interval,
            watch_system=self.tracks[SYSTEM].source is not None,
            watch_microphone=self.tracks[MIC].source is not None)
        self.watcher.prime()
        self.watcher.start()
        self._running = True
        return self.directory

    def _open_track(self, track: TrackState) -> bool:
        """Open one track, degrading gracefully if it is unavailable."""
        assert self.session is not None and self.watchdog is not None
        try:
            source = self._create_source(track.name)
            source.start()
        except AudioSourceError as exc:
            track.failure = str(exc)
            self._warn(f"{_label(track.name)} unavailable: {exc}")
            self.session.set(f"{track.name.lower()}_error", str(exc))
            return False
        except Exception as exc:  # pragma: no cover - unexpected driver fault
            track.failure = str(exc)
            self._warn(f"{_label(track.name)} unavailable: {exc}")
            return False

        health = source.health()
        max_channels = self.config.system_max_channels if track.name == SYSTEM else 1
        writer = TrackWriter(track.name, self.audio_dir, self.session,
                             channels=1, fmt=self.config.wav_format,
                             rotate_seconds=self.config.rotate_seconds)
        writer.start()
        pipeline = TrackPipeline(source, writer, sync=self.sync,
                                 max_channels=max_channels,
                                 subscribers=list(self._subscribers))
        pipeline.start()

        track.source, track.writer, track.pipeline = source, writer, pipeline
        track.device = health.device
        track.failure = None
        self.session.note_device(track.name, health.device, health.sample_rate,
                                 health.channels)
        self.watchdog.watch(track.name, source, writer)
        return True

    def _create_source(self, name: str) -> AudioSource:
        if name == SYSTEM:
            return self.backend.create_system_source(self.config.system_device)
        return self.backend.create_microphone_source(self.config.mic_device)

    def stop(self) -> SessionResult:
        """Stop cleanly and finalise the session."""
        if not self._running and self.session is None:
            raise RuntimeError("recorder was never started")
        self._stopping.set()
        self._running = False

        if self.watcher is not None:
            self.watcher.stop()
        if self.watchdog is not None:
            self.watchdog.stop()
        for thread in self._reopen_threads:
            if thread.is_alive():
                thread.join(timeout=REOPEN_DEADLINE_S * 2)

        for track in self.tracks.values():
            if track.source is not None:
                track.source.stop()
        for track in self.tracks.values():
            if track.pipeline is not None:
                track.pipeline.stop()
        for track in self.tracks.values():
            if track.writer is not None:
                track.writer.stop()

        return self._finalize()

    def _finalize(self) -> SessionResult:
        assert self.session is not None
        system_wav = mic_wav = mix_wav = None
        system_track = self.tracks[SYSTEM]
        mic_track = self.tracks[MIC]

        if system_track.writer is not None:
            system_wav = concat_segments(system_track.writer.segment_files,
                                         os.path.join(self.directory, "system.wav"),
                                         fmt=self.config.wav_format)
        if mic_track.writer is not None:
            mic_wav = concat_segments(mic_track.writer.segment_files,
                                      os.path.join(self.directory, "microphone.wav"),
                                      fmt=self.config.wav_format)
        if self.config.mix and (system_wav or mic_wav):
            offset = self.sync.relative_offset_seconds(MIC, SYSTEM) or 0.0
            mix_wav = mix_tracks(system_wav, mic_wav,
                                 os.path.join(self.directory, "meeting_mix.wav"),
                                 offset_seconds=offset, fmt=self.config.wav_format)

        drift = self.sync.reports()
        snapshot = self.session.snapshot()
        self.session.set("drift", drift)
        self.session.set("tracks", {
            "system": os.path.basename(system_wav) if system_wav else None,
            "microphone": os.path.basename(mic_wav) if mic_wav else None,
            "mix": os.path.basename(mix_wav) if mix_wav else None,
        })
        self.session.set_counters(
            dropouts=len(snapshot.get("dropouts", [])),
            device_changes=len(snapshot.get("device_changes", [])),
            warnings=self._warnings,
        )
        self.session.finish()

        return SessionResult(
            directory=self.directory,
            session_id=self.session.session_id,
            duration_s=self.session.elapsed(),
            system_wav=system_wav,
            microphone_wav=mic_wav,
            mix_wav=mix_wav,
            dropouts=len(snapshot.get("dropouts", [])),
            device_switches=len(snapshot.get("device_changes", [])),
            drift=drift,
        )

    # -- device changes --------------------------------------------------

    def _on_device_change(self, change: DeviceChange) -> None:
        """Log the change and reopen the affected stream (spec 6)."""
        if self.session is not None:
            self.session.add_device_change(
                change.type, change.from_device, change.to_device,
                source=change.source,
                from_sample_rate=change.from_sample_rate,
                to_sample_rate=change.to_sample_rate)
        if change.source == SYSTEM and not self.backend.system_capture_follows_default_endpoint:
            # Capture is endpoint-independent here; the change is history,
            # not an interruption.
            return
        if not change.requires_reopen:
            return
        self._schedule_reopen(change.source, change.type)

    def _schedule_reopen(self, name: str, reason: str) -> None:
        if self._stopping.is_set():
            return
        thread = threading.Thread(target=self._reopen, args=(name, reason),
                                  name=f"reopen-{name.lower()}", daemon=True)
        self._reopen_threads = [t for t in self._reopen_threads if t.is_alive()]
        self._reopen_threads.append(thread)
        thread.start()

    def _reopen(self, name: str, reason: str) -> None:
        """Close only the affected stream and reopen against the new endpoint.

        The session, the writers and the timeline all stay alive; the gap is
        timestamped and padded with silence so the tracks stay aligned.
        """
        track = self.tracks.get(name)
        if track is None or not track.enabled or self.session is None:
            return
        if not track.reopen_lock.acquire(blocking=False):
            return   # a reopen is already in flight for this track
        started = time.monotonic()
        old_device = track.device
        if self.watchdog is not None:
            self.watchdog.suspend(name)
        try:
            if track.source is not None:
                try:
                    track.source.stop()
                except Exception:
                    pass

            backoff = Backoff()
            source: Optional[AudioSource] = None
            last_error = ""
            while not self._stopping.is_set():
                try:
                    source = self._create_source(name)
                    source.start()
                    break
                except AudioSourceError as exc:
                    last_error = str(exc)
                    if not exc.recoverable:
                        break
                except Exception as exc:  # pragma: no cover - driver fault
                    last_error = str(exc)
                source = None
                if backoff.attempts >= 8:
                    break
                time.sleep(backoff.next_delay())

            duration = time.monotonic() - started
            if source is None:
                track.failure = last_error or "could not reopen the stream"
                self.session.add_dropout(name, f"{reason}: {track.failure}",
                                         round(duration, 3), recovered=False)
                self._warn(f"{_label(name)} could not be reopened: {track.failure}")
                return

            health = source.health()
            track.source = source
            track.device = health.device
            track.failure = None
            if track.pipeline is not None:
                # A plain reference swap: the pump thread picks up the new
                # source on its next read().
                track.pipeline.source = source
            if self.watchdog is not None:
                self.watchdog.replace_source(name, source)
            self.session.note_device(name, health.device, health.sample_rate,
                                     health.channels)
            self.session.add_dropout(name, reason, round(duration, 3),
                                     recovered=True, from_device=old_device,
                                     to_device=health.device)
            if duration > REOPEN_DEADLINE_S:
                self._warn(f"{_label(name)} took {duration:.1f}s to recover "
                           f"(target {REOPEN_DEADLINE_S:.0f}s)")
        finally:
            if self.watchdog is not None:
                self.watchdog.resume(name)
            track.reopen_lock.release()

    # -- watchdog hooks --------------------------------------------------

    def _on_outage(self, name: str, reason: str) -> None:
        if self.session is not None:
            self.session.add_dropout(name, reason, None, recovered=False)
        self._warn(f"{_label(name)} interrupted: {reason}")

    def _on_recovery(self, name: str, reason: str) -> None:
        if self.session is not None:
            self.session.add_dropout(name, reason, None, recovered=True)

    # -- status ----------------------------------------------------------

    def status(self) -> RecorderStatus:
        status = RecorderStatus(recording=self._running)
        if self.session is not None:
            status.elapsed_s = self.session.elapsed()
            snapshot = self.session.snapshot()
            status.dropouts = len(snapshot.get("dropouts", []))
            status.device_switches = len(snapshot.get("device_changes", []))

        for name, track in self.tracks.items():
            if track.pipeline is None:
                continue
            meter = track.pipeline.meter
            if name == SYSTEM:
                status.system_bar = meter.bar()
                status.system_db = meter.rms_db
                status.system_device = track.device
            else:
                status.mic_bar = meter.bar()
                status.mic_db = meter.rms_db
                status.mic_device = track.device

        if self.watchdog is not None:
            health = self.watchdog.health()
            status.queue_overruns = int(health.get("queue_overruns", 0) or 0)
            warning = health.get("disk_warning")
            if warning:
                status.warnings.append(str(warning))
        status.warnings.extend(self._warnings[-3:])
        return status

    def health(self) -> Dict[str, object]:
        return self.watchdog.health() if self.watchdog is not None else {}

    # -- helpers ---------------------------------------------------------

    def _warn(self, message: str) -> None:
        self._warnings.append(message)

    def _make_session_dir(self) -> str:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        slug = _slugify(self.config.title) if self.config.title else ""
        name = f"{stamp}_{slug}" if slug else stamp
        path = os.path.join(self.config.output_dir, name)
        suffix = 1
        while os.path.exists(path):
            suffix += 1
            path = os.path.join(self.config.output_dir, f"{name}-{suffix}")
        os.makedirs(path, exist_ok=True)
        return path


def _slugify(title: Optional[str]) -> str:
    if not title:
        return ""
    slug = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE).strip().lower()
    return re.sub(r"[\s_-]+", "_", slug)[:60]


def _label(name: str) -> str:
    return "System audio" if name == SYSTEM else "Microphone"


def permission_blocked(reports: List[PermissionReport]) -> bool:
    """True when *every* capability is blocked by permissions or support."""
    return bool(reports) and all(
        r.status in (PermissionStatus.SUPPORTED_BUT_PERMISSION_MISSING,
                     PermissionStatus.UNSUPPORTED,
                     PermissionStatus.DEPENDENCY_MISSING)
        for r in reports)
