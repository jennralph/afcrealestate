# Windows process-loopback helper (optional)

Spec section 7 asks for process loopback to be *investigated*, explicitly not
as an MVP prerequisite. The MVP path is WASAPI shared-mode loopback on the
default render endpoint (`meetingcap/audio/windows.py`), which needs no
helper and no elevation.

This directory is where an optional native helper goes if you decide to build
one. `meetingcap/audio/windows_process_loopback.py` looks for
`meetingcap-process-loopback.exe` here (or wherever
`MEETINGCAP_PROCESS_LOOPBACK_HELPER` points) and uses it only when the OS
supports it. When it is absent — the normal case — the recorder falls through
the hierarchy in spec 7:

1. native process loopback (helper, if present and supported)
2. default WASAPI loopback
3. re-discovered WASAPI output endpoint
4. graceful error

## Why a helper

`ActivateAudioInterfaceAsync` with `VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK`
requires a COM activation-completion callback and an
`AUDIOCLIENT_ACTIVATION_PARAMS` blob. That is a poor fit for ctypes, so the
activation lives in a small native binary. Only documented Windows APIs are
involved, and the helper runs as an ordinary user.

Requires Windows 10 build 20348 / Windows 11 or newer.

## Contract

```
meetingcap-process-loopback.exe --exclude-pid <pid> --rate 48000 --channels 2 --format f32
```

* **stdout** — interleaved little-endian float32 frames, nothing else
* **stderr** — diagnostics
* **exit code** — non-zero if activation fails

`--exclude-pid` selects `PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE`,
i.e. "capture everything except our own process tree", which is what keeps
the recorder's own audio out of the capture while still recording every
meeting application.

## When it is worth building

Process loopback is not tied to a physical endpoint, so it can survive an
output-device change without a reopen. Prefer it only if testing on your
target Windows builds proves it more reliable than endpoint loopback; the
endpoint path already meets the acceptance criteria.
