// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "Meridian",
    platforms: [.macOS(.v15)],
    dependencies: [
        .package(url: "https://github.com/gonzalezreal/swift-markdown-ui", exact: "2.4.1"),
        .package(url: "https://github.com/Lakr233/libghostty-spm", revision: "bef6a409a2de17f5e0e81027c15f4bb488dc2cec")
    ],
    targets: [
        .executableTarget(name: "Meridian", dependencies: [
            .product(name: "MarkdownUI", package: "swift-markdown-ui"),
            .product(name: "GhosttyTerminal", package: "libghostty-spm")
        ], path: "Sources", exclude: ["streams.py", "workstream_log.py", "workstream_links.py", "subagents.py", "agent_navigation.py", "codex_session.py", "usage_limits.py",
                       "pipeline.py", "pipeline_tracker.py",
                       "manager.py", "manager_store.py", "manager_brain.py", "manager_role.md"], resources: [.process("AgentIcons.xcassets")])
    ],
    swiftLanguageModes: [.v5]
)
