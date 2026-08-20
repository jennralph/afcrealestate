# Zero-Admin AI Meeting Recorder — Phase 1: audio acquisition

A desktop meeting recorder that captures **system audio** and the
**microphone** as two independent tracks, follows the user's audio
environment across device changes, and **never requests administrator or root
privileges**.

This repository currently implements **Phase 1** of the engineering
specification — the audio engine — which the spec requires to be reliable
before transcription and AI notes are built on top of it (§22, §46). The
boundary those later phases plug into is already here and tested
(`Recorder.subscribe`, §22).

```
python meeting_capture.py
```

## What works today

| Spec | Capability | Status |
| --- | --- | --- |
| §3–§4 | `AudioSource` contract + platform adapters selected by `platform.system()` | done |
| §5 | Windows WASAPI shared-mode loopback via PyAudioWPatch, no Stereo Mix / VB-Cable / VoiceMeeter | implemented, untested off-Windows |
| §6 | `DeviceWatcher`: default output/input change, device loss, sample-rate change → reopen without ending the meeting | done |
| §7 | `WindowsProcessLoopback` behind the documented fallback hierarchy | optional scaffold + helper contract |
| §8 | macOS ScreenCaptureKit (Swift helper), no BlackHole/Loopback/Soundflower | implemented, uncompiled |
| §9 | Linux PipeWire sink monitor, PulseAudio fallback, dynamic sink discovery | done |
| §10–§12 | Bounded queues, callbacks that only copy, 48 kHz float32 normalisation, monotonic timestamps, drift monitoring | done |
| §13–§14 | Separate MIC/SYSTEM tracks (never summed at equal volume), RMS diagnostics that never stop the recording | done |
| §15 | Watchdog with the 0.1/0.25/0.5/1/2 s backoff, stall detection, disk-space checks | done |
| §16–§17 | Segmented crash-safe recording + `session.json` | done |
| §18–§19 | CLI and console UX | done |
| §20–§21 | Permission reporting, no surprise installation | done |
| §43 | `--diagnostics` self-test | done |

Not started (later phases, in the spec's own order): transcription,
multilingual transcripts, AI notes, the interactive workspace, natural-language
editing, provider abstraction, cross-meeting memory.

## The security constraint

The application never elevates and never bypasses an OS control. It does not
use sudo or UAC, install kernel or virtual audio drivers, modify TCC or
Windows privacy settings, inject into meeting applications, or hide that
recording is active.

"Zero-admin" means *ordinary-user operation after legitimate OS permissions
have been granted*. When a permission is missing, the recorder says which one
and where to grant it — never a stack trace, never a workaround:

```
System audio: SUPPORTED_BUT_PERMISSION_MISSING
  System audio capture is supported, but macOS has not granted
  Screen & System Audio Recording permission.
  Open: System Settings -> Privacy & Security -> Screen & System Audio Recording
  No administrator password is required by this application.
```

## Install

Dependencies are detected, never installed. If something is missing the
program prints one copyable command and exits.

**Windows**
```
py -m venv .venv
.venv\Scripts\python -m pip install numpy PyAudioWPatch
```

**macOS**
```
python3 -m venv .venv
.venv/bin/python -m pip install numpy sounddevice
```
then build the system-audio helper — see `helpers/macos/README.md`.

**Linux** (PipeWire or PulseAudio, already present on a normal desktop)
```
python3 -m venv .venv
.venv/bin/python -m pip install numpy
```

No administrator shell in any case.

## Usage

```
python meeting_capture.py                      # record until Ctrl+C
python meeting_capture.py --list-devices
python meeting_capture.py --output ./meetings
python meeting_capture.py --mic auto
python meeting_capture.py --system auto
python meeting_capture.py --no-mix
python meeting_capture.py --diagnostics
```

Also available:

```
--check                 report permissions and dependencies, then exit
--title "ACME call"     name the session directory
--duration 3600         stop automatically
--source synthetic      record a generated signal; no hardware or drivers
--wav-format float32    write float32 WAVs instead of 16-bit PCM
--repair ./meetings/…   finalise segments left by a killed process
--non-interactive       never wait for ENTER
```

`--source synthetic` is how the pipeline can be exercised on a machine with
no sound card, including CI.

## What a session looks like

```
meetings/2026-08-20_141530_acme_management_call/
    session.json          devices, device changes, dropouts, drift, segments
    system.wav            everything the computer played (the other participants)
    microphone.wav        the user
    meeting_mix.wav       convenience mix, aligned on the monotonic clock
    audio/
        system_0001.wav   crash-safe segments, rotated ~5 minutes
        mic_0001.wav
```

`system.wav` and `microphone.wav` stay on one timeline: a gap caused by a
device switch is padded with silence rather than closed up, so a timestamp
means the same thing on both tracks.

## Design in one page

```
SYSTEM CALLBACK ──► bounded queue ──► normalise 48k f32 ──► segment writer ──► system_NNNN.wav
MIC CALLBACK    ──► bounded queue ──► normalise 48k f32 ──► segment writer ──► mic_NNNN.wav
                                            │
                                            ├──► level meters (RMS/peak)
                                            ├──► drift monitor (monotonic clock)
                                            └──► on_audio_chunk subscribers  ← transcription plugs in here

DeviceWatcher (1 Hz) ──► reopen the affected stream, keep the meeting alive
Watchdog     (1 Hz) ──► restart a dead or stalled stream, 0.1→0.25→0.5→1→2→5 s
```

Callbacks only copy frames. Resampling, metering, disk I/O and any
subscriber work happen on other threads — never on a driver's thread.

| Module | Role |
| --- | --- |
| `meetingcap/audio/base.py` | `AudioSource`, `AudioChunk`, `SourceHealth`, queue accounting |
| `meetingcap/audio/{windows,macos,linux,synthetic}.py` | platform adapters |
| `meetingcap/audio/registry.py` | `platform.system()` → backend, once |
| `meetingcap/capture/pipeline.py` | pump, normalisation, meters, subscriber fan-out |
| `meetingcap/capture/writer.py` | segmented WAV writing, gap padding, rotation |
| `meetingcap/capture/wavio.py` | RIFF writer/reader and crash repair |
| `meetingcap/capture/watcher.py` | device-change detection, backoff schedule |
| `meetingcap/capture/watchdog.py` | stream supervision and restart |
| `meetingcap/capture/recorder.py` | session lifecycle and recovery orchestration |
| `meetingcap/permissions.py` | the four permission states, and how to fix each |

## Extending: the transcription boundary (§22)

The recorder has no opinion about speech-to-text:

```python
from meetingcap.capture.recorder import Recorder, RecorderConfig

recorder = Recorder(RecorderConfig(output_dir="./meetings"))
recorder.subscribe(lambda source, pcm, timestamp_ns: ...)   # mono float32 @ 48 kHz
recorder.start()
```

`source` is `"SYSTEM"` or `"MIC"` — which is also the first speaker
distinction the spec asks for (§13, §26): SYSTEM is *others*, MIC is *me*.

## Tests

```
python -m pip install pytest
python -m pytest
```

103 tests, no hardware required. They cover resampling continuity, the WAV
crash-repair path, gap padding, the backoff schedule, device-switch recovery,
watchdog restarts, Windows loopback device resolution (against a fake
PyAudioWPatch), PipeWire/PulseAudio discovery (against fake `pactl` output),
the CLI and the diagnostics.

## Verification status

Verified here: everything exercised by the test suite and by
`--source synthetic` on Linux, including a full record → device switch →
recover → finalise cycle.

Not verified here: real WASAPI loopback, ScreenCaptureKit and PipeWire
capture, which need Windows, macOS and a Linux desktop with an audio server
respectively. This container has none. The testing matrix in spec §44 —
speakers, wired, USB, Bluetooth, HDMI, Zoom/Teams/Meet/Discord, and switching
devices mid-recording — remains to be run on real hardware; start with
`python meeting_capture.py --diagnostics`.
