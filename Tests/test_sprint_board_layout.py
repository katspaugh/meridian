"""Render the production SwiftUI board on macOS and check its geometry.

Run: python3 Tests/test_sprint_board_layout.py
Requires a logged-in macOS GUI session and Xcode command line tools.
"""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'Sources/SprintBoard.swift').read_text()
# Observe production layout without adding test hooks to the app.
source = source.replace(
    '.font(.callout.weight(.semibold)).padding(16).background(.bar)',
    '.font(.callout.weight(.semibold)).padding(16).background(.bar)'
    '.onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { Probe.header = $0 }',
)
source = source.replace(
    '.defaultScrollAnchor(.topLeading)',
    '.defaultScrollAnchor(.topLeading)'
    '.onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { Probe.viewport = $0 }',
)
source = source.replace(
    '}.frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)',
    '}.frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)'
    '.onChange(of: visibleStreams.count, initial: true) { _, count in Probe.visibleCount = count }',
)
harness = r'''
import AppKit
import SwiftUI

// Lightweight model fixtures; the view itself is compiled from production source.
struct BeadsTask: Identifiable {
    let id = UUID().uuidString
    var title = "Write the implementation plan for the pipeline and documents"
    var status = "in_progress"
    var blockerReason: String? { nil }
    var blockerAction: String? { nil }
}
struct Stream: Identifiable {
    let id = UUID().uuidString
    var title = "Show Superpowers specs, plans and pipeline"
    var folder = "/tmp/workstream"
    var reference = ""
    var state = "Working"
    var tasks = [BeadsTask()]
    var displayState: String { state }
}
struct TaskBlockerDetails: View {
    let task: BeadsTask
    var body: some View { EmptyView() }
}
func stateColor(_ state: String) -> Color { .blue }
enum Probe {
    static var visibleCount = -1
    static var header = CGRect.zero
    static var viewport = CGRect.zero
}

@main
struct LayoutTest {
    @MainActor static func settle() {
        RunLoop.main.run(until: Date().addingTimeInterval(0.3))
    }
    @MainActor static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1400, height: 1000),
                              styleMask: [.titled, .resizable], backing: .buffered, defer: false)
        let host = NSHostingView(rootView: SprintBoard(streams: [], openStream: { _ in }, newStream: {}))
        window.contentView = host
        window.orderFront(nil)
        for count in [2, 20, 1] {
            host.rootView = SprintBoard(streams: (0..<count).map { _ in Stream() }, openStream: { _ in }, newStream: {})
            for size in [NSSize(width: 1400, height: 1000), NSSize(width: 800, height: 1200), NSSize(width: 1800, height: 600)] {
                window.setContentSize(size)
                settle()
                precondition(Probe.visibleCount == count)
                if count == 2 && size.width == 1400,
                   let snapshot = ProcessInfo.processInfo.environment["SPRINT_BOARD_SNAPSHOT"],
                   let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) {
                    host.cacheDisplay(in: host.bounds, to: bitmap)
                    try! bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: snapshot))
                }
                let gap = Probe.header.minY - Probe.viewport.minY
                guard Probe.viewport.height > 0, abs(gap) < 2 else {
                    fatalError("\(count) rows at \(size): header gap \(gap), header \(Probe.header), viewport \(Probe.viewport)")
                }
                print("PASS: \(count) rows, \(size), top gap \(gap)")
            }
        }
        // A completed workstream must be excluded by the initial toggle state.
        Probe.header = .zero
        host.rootView = SprintBoard(streams: [Stream(state: "Done")], openStream: { _ in }, newStream: {})
        settle()
        precondition(Probe.visibleCount == 0, "Completed stream is visible by default")
        print("PASS: completed workstreams hidden by default")
        host.rootView = SprintBoard(streams: [], openStream: { _ in }, newStream: {})
        settle()
        print("PASS: empty board renders")
        window.orderOut(nil)
    }
}
'''
with tempfile.TemporaryDirectory(prefix='sprint-layout-') as directory:
    directory = Path(directory)
    view = directory / 'SprintBoard.swift'
    main = directory / 'Harness.swift'
    binary = directory / 'layout-test'
    view.write_text(source)
    main.write_text(harness)
    subprocess.run(['xcrun', 'swiftc', '-parse-as-library', str(view), str(main), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
