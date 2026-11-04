// swift-tools-version:5.10
import PackageDescription

let package = Package(
    name: "WM",
    platforms: [.macOS(.v14)],
    dependencies: [
        .package(url: "https://github.com/migueldeicaza/SwiftTerm", exact: "1.20.0"),
    ],
    targets: [
        .executableTarget(
            name: "WM",
            dependencies: [.product(name: "SwiftTerm", package: "SwiftTerm")],
            path: "Sources/WM"
        ),
    ]
)
