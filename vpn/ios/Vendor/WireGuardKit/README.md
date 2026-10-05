# Vendored WireGuardKit

Source: `https://git.zx2c4.com/wireguard-apple` at `2fec12a` (2023-02-15), MIT
licensed (see `COPYING`).

Local changes:

1. `Package.swift` links the Go bridge as a prebuilt `WireGuardKitGo.xcframework`
   (built by `../../scripts/build-wireguard-go.sh`) instead of needing an
   external-build-system Xcode target. The xcframework covers device and
   simulator, which the upstream Makefile did not.
2. `GoBridge/go.mod` moves to `golang.zx2c4.com/wireguard` 2025-05-21 and
   `golang.org/x/sys` v0.33.0 so the bridge builds with Go 1.23–1.25.
3. `Sources/WireGuardKitC/WireGuardKitC.h` includes `<sys/types.h>`; Xcode 16
   SDKs no longer provide `u_int32_t` implicitly.

`GoBridge/goruntime-boottime-over-monotonic.diff` is upstream's patch making Go
timers count time the phone spent asleep, so WireGuard re-handshakes promptly
after wake. The build script applies it to a private copy of GOROOT.
