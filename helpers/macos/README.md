# macOS system-audio helper

`SystemAudioCapture.swift` captures system audio with Apple's native
**ScreenCaptureKit** and streams raw float32 frames to stdout, where
`meetingcap/audio/macos.py` picks them up.

No virtual audio driver is installed — not BlackHole, Loopback or
Soundflower — and no administrator password is required at any point.

## Build

Requires Xcode command line tools (`xcode-select --install`) and macOS 13
(Ventura) or newer.

```
cd helpers/macos
swiftc -O -framework ScreenCaptureKit -framework AVFoundation \
    -o meetingcap-system-audio SystemAudioCapture.swift
```

The recorder finds the binary automatically at `helpers/macos/meetingcap-system-audio`.
To keep it elsewhere, point the recorder at it:

```
export MEETINGCAP_MACOS_HELPER=/path/to/meetingcap-system-audio
```

## Permissions

The first run asks for **Screen & System Audio Recording**. Grant it to the
application that launches the recorder (Terminal, iTerm, or your packaged
app):

    System Settings -> Privacy & Security -> Screen & System Audio Recording

Microphone capture separately requires:

    System Settings -> Privacy & Security -> Microphone

These are ordinary OS privacy controls. The recorder detects their state and
tells you what is missing; it never modifies TCC and never tries to work
around a denial.

## Protocol

```
meetingcap-system-audio --rate 48000 --channels 2
```

* **stdout** — interleaved little-endian float32 frames, nothing else
* **stderr** — one diagnostic line per event
* **exit code** — non-zero when the stream cannot start (permission denied,
  no display, unsupported macOS version)

Any helper honouring this contract can be substituted.

## Status

The Swift source has not been compiled or run in this repository's CI, which
has no macOS host. Treat the first build on a Mac as the point where it gets
verified — start with `python meeting_capture.py --diagnostics`, which
measures the actual RMS coming out of the helper.
