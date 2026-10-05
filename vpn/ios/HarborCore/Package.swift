// swift-tools-version:5.9
// Platform-neutral logic shared by the app, the packet tunnel and the widgets.
// It imports only Foundation, so `swift test` runs it on macOS or Linux.
import PackageDescription

let package = Package(
    name: "HarborCore",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [.library(name: "HarborCore", targets: ["HarborCore"])],
    targets: [
        .target(name: "HarborCore"),
        .testTarget(name: "HarborCoreTests", dependencies: ["HarborCore"]),
    ]
)
