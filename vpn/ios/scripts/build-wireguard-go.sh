#!/usr/bin/env bash
# Build the wireguard-go bridge into Vendor/WireGuardKit/WireGuardKitGo.xcframework
# for iPhone and the iOS Simulator. Needs Xcode and Go 1.23-1.25 on macOS.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG="$ROOT/Vendor/WireGuardKit"
BRIDGE="$PKG/GoBridge"
BUILD="$PKG/.build-go"
OUT="$PKG/WireGuardKitGo.xcframework"
MIN_IOS="${MIN_IOS:-17.0}"

command -v go >/dev/null || { echo "Go is required: brew install go" >&2; exit 1; }
REAL_GOROOT="$(GOTOOLCHAIN=local go env GOROOT)"

# Private GOROOT with the boottime patch so timers survive device sleep.
GOROOT_COPY="$BUILD/goroot"
STAMP="$GOROOT_COPY/.harbor-prepared-$(GOTOOLCHAIN=local go env GOVERSION)"
if [[ ! -f "$STAMP" ]]; then
  rm -rf "$GOROOT_COPY"
  mkdir -p "$GOROOT_COPY"
  rsync -a --exclude=pkg/obj/go-build "$REAL_GOROOT/" "$GOROOT_COPY/"
  patch -p1 -f -N -r- -d "$GOROOT_COPY" <"$BRIDGE/goruntime-boottime-over-monotonic.diff"
  touch "$STAMP"
fi
export GOROOT="$GOROOT_COPY" GOTOOLCHAIN=local CGO_ENABLED=1 GOOS=ios
GO="$GOROOT/bin/go"

build() { # sdk goarch clang-target outdir
  local sdk="$1" goarch="$2" target="$3" out="$4"
  local sysroot cc
  sysroot="$(xcrun --sdk "$sdk" --show-sdk-path)"
  cc="$(xcrun --sdk "$sdk" --find clang)"
  mkdir -p "$out"
  echo "==> $target"
  (cd "$BRIDGE" && \
    CC="$cc" GOARCH="$goarch" \
    CGO_CFLAGS="-isysroot $sysroot -target $target -fembed-bitcode=off" \
    CGO_LDFLAGS="-isysroot $sysroot -target $target" \
    "$GO" build -trimpath -buildvcs=false -ldflags=-w -buildmode=c-archive -o "$out/libwg-go.a" .)
  rm -f "$out/libwg-go.h"
}

rm -rf "$BUILD/lib" "$OUT"
build iphoneos        arm64 "arm64-apple-ios$MIN_IOS"            "$BUILD/lib/ios-arm64"
build iphonesimulator arm64 "arm64-apple-ios$MIN_IOS-simulator"  "$BUILD/lib/sim-arm64"
build iphonesimulator amd64 "x86_64-apple-ios$MIN_IOS-simulator" "$BUILD/lib/sim-x86_64"

mkdir -p "$BUILD/lib/sim"
lipo -create -output "$BUILD/lib/sim/libwg-go.a" "$BUILD/lib/sim-arm64/libwg-go.a" "$BUILD/lib/sim-x86_64/libwg-go.a"

HEADERS="$BUILD/headers"
rm -rf "$HEADERS" && mkdir -p "$HEADERS"
cp "$BRIDGE/wireguard.h" "$HEADERS/"
cat >"$HEADERS/module.modulemap" <<'EOF'
module WireGuardKitGo {
    umbrella header "wireguard.h"
    export *
}
EOF

xcodebuild -create-xcframework \
  -library "$BUILD/lib/ios-arm64/libwg-go.a" -headers "$HEADERS" \
  -library "$BUILD/lib/sim/libwg-go.a" -headers "$HEADERS" \
  -output "$OUT"
echo "Built $OUT"
