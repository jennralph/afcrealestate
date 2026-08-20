"""Session metadata — ``session.json`` (spec 17).

The file is rewritten atomically after every material event so that a crash
leaves a valid document behind, never a half-written one.
"""

from __future__ import annotations

import json
import os
import platform
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SessionMetadata:
    """Thread-safe accumulator for everything we learned during a session."""

    def __init__(self, directory: str, *, session_id: Optional[str] = None,
                 title: Optional[str] = None) -> None:
        self.directory = directory
        self.path = os.path.join(directory, "session.json")
        self._lock = threading.Lock()
        # Both writer threads (and the watchdog) save concurrently, so the
        # write-and-rename is serialised and each attempt uses its own temp
        # file: two threads sharing one temp name would race on os.replace.
        self._save_lock = threading.Lock()
        self.last_save_error: Optional[str] = None
        self._t0 = time.monotonic()
        self._data: Dict[str, Any] = {
            "session_id": session_id or uuid.uuid4().hex,
            "title": title,
            "app_version": _app_version(),
            "started_at": _utc_now(),
            "started_monotonic_ns": time.monotonic_ns(),
            "platform": f"{platform.system()} {platform.release()}",
            "platform_details": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
                "python": platform.python_version(),
            },
            "administrator_required": False,
            "system_devices": [],
            "microphone_devices": [],
            "device_changes": [],
            "dropouts": [],
            "sample_rates": {},
            "segments": [],
            "counters": {},
            "ended_at": None,
        }
        os.makedirs(directory, exist_ok=True)
        self.save()

    # -- accessors ------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._data["session_id"]

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def elapsed(self) -> float:
        return round(time.monotonic() - self._t0, 2)

    # -- mutation -------------------------------------------------------

    def note_device(self, source: str, device: str, sample_rate: int = 0,
                    channels: int = 0) -> None:
        """Record a device the session has used.  Idempotent per device."""
        key = "system_devices" if source == "SYSTEM" else "microphone_devices"
        with self._lock:
            entries: List[Dict[str, Any]] = self._data[key]
            for entry in entries:
                if entry["name"] == device:
                    entry["last_seen"] = self.elapsed()
                    break
            else:
                entries.append({
                    "name": device,
                    "sample_rate": sample_rate,
                    "channels": channels,
                    "first_seen": self.elapsed(),
                    "last_seen": self.elapsed(),
                })
            if sample_rate:
                self._data["sample_rates"][device] = sample_rate
        self.save()

    def add_device_change(self, change_type: str, from_device: str,
                          to_device: str, **extra: Any) -> Dict[str, Any]:
        """Append a device-change event (spec 17 example shape)."""
        event = {
            "time": self.elapsed(),
            "type": change_type,
            "from": from_device,
            "to": to_device,
        }
        event.update(extra)
        with self._lock:
            self._data["device_changes"].append(event)
        self.save()
        return event

    def add_dropout(self, source: str, reason: str, duration_s: Optional[float] = None,
                    **extra: Any) -> Dict[str, Any]:
        """Timestamp an interruption without ending the logical meeting."""
        event = {
            "time": self.elapsed(),
            "source": source,
            "reason": reason,
            "duration_s": duration_s,
        }
        event.update(extra)
        with self._lock:
            self._data["dropouts"].append(event)
        self.save()
        return event

    def add_segment(self, source: str, filename: str, start_monotonic_ns: int,
                    sample_rate: int, channels: int) -> Dict[str, Any]:
        entry = {
            "source": source,
            "file": filename,
            "start_monotonic_ns": start_monotonic_ns,
            "start_time": self.elapsed(),
            "sample_rate": sample_rate,
            "channels": channels,
            "frames": 0,
            "closed": False,
        }
        with self._lock:
            self._data["segments"].append(entry)
        self.save()
        return entry

    def close_segment(self, filename: str, frames: int) -> None:
        with self._lock:
            for entry in self._data["segments"]:
                if entry["file"] == filename:
                    entry["frames"] = frames
                    entry["closed"] = True
                    break
        self.save()

    def set_counters(self, **counters: Any) -> None:
        with self._lock:
            self._data["counters"].update(counters)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
        self.save()

    def finish(self) -> None:
        with self._lock:
            self._data["ended_at"] = _utc_now()
            self._data["duration_s"] = self.elapsed()
        self.save()

    # -- persistence ----------------------------------------------------

    def save(self) -> None:
        """Rewrite ``session.json`` atomically.

        Metadata is secondary to audio: a failure here is recorded and
        reported, but it must never propagate into a capture or writer
        thread and cost the recording.
        """
        with self._save_lock:
            with self._lock:
                payload = json.dumps(self._data, indent=2, ensure_ascii=False)
            tmp = f"{self.path}.{os.getpid()}.{threading.get_ident():x}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.flush()
                    try:
                        os.fsync(fh.fileno())
                    except OSError:  # pragma: no cover - platform dependent
                        pass
                os.replace(tmp, self.path)
                self.last_save_error = None
            except OSError as exc:
                self.last_save_error = str(exc)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    @staticmethod
    def load(path: str) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)


def _app_version() -> str:
    from meetingcap import __version__
    return __version__
