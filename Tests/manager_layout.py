"""Compile the production manager slot and render its five states in an offscreen window.
Run on macOS: python3 Tests/manager_layout.py. PNGs go to /tmp/mux-manager-layout.
"""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'Sources/ManagerStore.swift').read_text()
source = 'import SwiftUI\nimport AppKit\n' + source[source.index('struct ManagerAskDetails:'):]
source = source.replace('.frame(height: 120, alignment: .top).clipped().background(.bar)',
    '.frame(height: 120, alignment: .top).clipped().background(.bar)'
    '.onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { Probe.slot = $0 }')
harness = r'''
import AppKit
import SwiftUI
struct WorkstreamTerminal { let id = "t1"; let live = true; let state = "Review"; let provider = "claude" }
struct Stream { let id = "w1"; let terminals = [WorkstreamTerminal()] }
struct ManagerAsk: Identifiable { let options = ["Stable", "Beta"]; let recommendation = "Beta"; let id = 1; let terminal = "t1"; let summary = "Which release channel should receive this build?" }
struct State { var messages = [ManagerMessage]() }
@MainActor final class ManagerStore: ObservableObject {
    @Published var state: State? = State()
    @Published var focusedAsk: Int?
    func act(_ m: ManagerMessage, _ action: String, index: Int? = nil) async -> Bool { true }
    func dismiss(_ ask: ManagerAsk) async {}
    func readAsk(_ ask: ManagerAsk) async {}
    func askAction(_ ask: ManagerAsk, _ action: String) async {}
}
enum Probe { static var slot = CGRect.zero; static var footer = CGRect.zero }
struct Inspector: View {
    @ObservedObject var store: ManagerStore
    let asks: [ManagerAsk]
    var body: some View {
        VStack(spacing: 0) {
            Text("Tasks").font(.headline).padding()
            Text("Implement the proactive manager").padding()
            Spacer()
            Divider()
            ManagerSlot(stream: Stream(), asks: asks, store: store, openTerminal: { _ in })
            Divider()
            Text("● Working · Running tests · 3m").font(.callout).padding()
                .onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { Probe.footer = $0 }
        }.frame(width: 300, height: 460).background(Color(nsColor: .windowBackgroundColor))
    }
}
@main struct Main {
    @MainActor static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 300, height: 460),
                              styleMask: [.titled], backing: .buffered, defer: false)
        let store = ManagerStore()
        let host = NSHostingView(rootView: Inspector(store: store, asks: []))
        window.contentView = host
        window.setFrameOrigin(NSPoint(x: -10000, y: -10000))
        window.orderFront(nil)
        let directory = URL(fileURLWithPath: "/tmp/mux-manager-layout")
        try! FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        var originalFooter: CGFloat?
        for name in ["empty", "status", "multiple", "question", "long"] {
            let button = ManagerButton(label: "Merge", action: "tell", terminal: "t1", value: "Merge the PR")
            let later = ManagerButton(label: "Later", action: "later", terminal: "", value: "")
            let open = ManagerButton(label: "Open", action: "open", terminal: "t1", value: "")
            let texts = name == "multiple" ? ["PR #123 checks passed. Merge it?", "Tests pass.", "Review is waiting."] :
                [name == "long" ? String("The new build passed all checks and is ready for your review; open the pull request to inspect changes.".prefix(100)) : "Tests pass. Nothing needed from you."]
            store.state = State(messages: name == "empty" || name == "question" ? [] : texts.enumerated().map { index, text in
                ManagerMessage(id: index + 1, revision: 1, time: Double(index), stream: "w1", terminal: "t1",
                               kind: name == "status" ? "status" : "next", text: text, rule: "", seen: false,
                               buttons: name == "status" ? [] : [button, open, later])
            })
            let host = NSHostingView(rootView: Inspector(store: store, asks: name == "question" ? [ManagerAsk()] : []))
            window.contentView = host
            RunLoop.main.run(until: Date().addingTimeInterval(0.3))
            host.layoutSubtreeIfNeeded()
            precondition(abs(Probe.slot.height - 120) < 1, "Slot changed height: \(Probe.slot)")
            if let originalFooter { precondition(abs(Probe.footer.minY - originalFooter) < 1, "Footer moved") }
            originalFooter = Probe.footer.minY
            let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds)!
            host.cacheDisplay(in: host.bounds, to: bitmap)
            try! bitmap.representation(using: .png, properties: [:])!.write(to: directory.appendingPathComponent(name + ".png"))
            print("PASS: \(name), slot height \(Probe.slot.height), footer y \(Probe.footer.minY)")
        }
        window.orderOut(nil)
    }
}
'''
with tempfile.TemporaryDirectory(prefix='manager-layout-') as directory:
    directory = Path(directory)
    view, main, binary = directory / 'Slot.swift', directory / 'Harness.swift', directory / 'check'
    view.write_text(source)
    main.write_text(harness)
    subprocess.run(['xcrun', 'swiftc', '-parse-as-library', str(view), str(main), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
