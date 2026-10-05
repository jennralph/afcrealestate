// swift-tools-version:5.9
//
// WireGuardKit from https://git.zx2c4.com/wireguard-apple (MIT, see COPYING),
// vendored because upstream has been dormant since 2023: its SwiftPM recipe
// needs a hand-made Xcode build target, does not build the Go bridge for the
// simulator, and pins a wireguard-go that no longer builds with current Go.
//
// Here the Go bridge is prebuilt into WireGuardKitGo.xcframework by
// ../../scripts/build-wireguard-go.sh (run it once after cloning, and after
// changing GoBridge/). Local changes to upstream are listed in README.md.
import PackageDescription

let package = Package(
    name: "WireGuardKit",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [
        .library(name: "WireGuardKit", targets: ["WireGuardKit"])
    ],
    targets: [
        .target(
            name: "WireGuardKit",
            dependencies: ["WireGuardKitGo", "WireGuardKitC"]
        ),
        .target(
            name: "WireGuardKitC",
            publicHeadersPath: "."
        ),
        .binaryTarget(
            name: "WireGuardKitGo",
            path: "WireGuardKitGo.xcframework"
        ),
    ]
)
