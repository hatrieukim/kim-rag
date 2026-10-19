// swift-tools-version:5.10
import PackageDescription

let package = Package(
    name: "KimRagMac",
    platforms: [.macOS("14.4")],
    targets: [
        .target(name: "KimRagCore"),
        .executableTarget(name: "KimRagMac", dependencies: ["KimRagCore"]),
        .testTarget(name: "KimRagCoreTests", dependencies: ["KimRagCore"]),
    ]
)
