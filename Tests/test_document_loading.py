"""Exercise the document view's loading lifecycle in a real SwiftUI host on macOS.

Run: python3 Tests/test_document_loading.py
Markdown rendering and app command keys are omitted so this needs only the SDK.
"""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / 'Sources/DocumentWindow.swift').read_text()
source = source.replace('import MarkdownUI', '').replace('Markdown(text)', 'Text(text)')
source = '\n'.join(line for line in source.splitlines() if not any(
    modifier in line for modifier in (
        '.markdownTextStyle', '.markdownImageProvider', '.markdownInlineImageProvider',
        '.focusedSceneValue',
    )
))
source = source.replace('checked = true', 'checked = true; Probe.results.append(text)')

harness = r'''
import AppKit
import SwiftUI

enum Probe { static var results: [String?] = [] }

@main
struct DocumentLoadingTest {
    @MainActor static func waitFor(_ description: String, _ condition: () -> Bool) {
        let deadline = Date().addingTimeInterval(4)
        while !condition() && Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        }
        guard condition() else { fatalError(description) }
        print("PASS: \(description)")
    }

    @MainActor static func main() throws {
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("spec.md")
        try "# Spec content".write(to: url, atomically: true, encoding: .utf8)
        let host = NSHostingView(rootView: DocumentWindow(url: url))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 820),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = host
        window.orderFront(nil)
        defer { window.orderOut(nil) }
        waitFor("document loads on first presentation") { Probe.results.last == "# Spec content" }
        try "# Updated spec".write(to: url, atomically: true, encoding: .utf8)
        waitFor("document reloads after editing") { Probe.results.last == "# Updated spec" }
        try FileManager.default.removeItem(at: url)
        waitFor("deleted document reaches unavailable state") { !Probe.results.isEmpty && Probe.results.last! == nil }
        try "# Restored spec".write(to: url, atomically: true, encoding: .utf8)
        waitFor("document recovers when restored") { Probe.results.last == "# Restored spec" }
        Probe.results = []
        host.rootView = DocumentWindow(url: directory.appendingPathComponent("missing.md"))
        waitFor("missing document finishes its initial check") { !Probe.results.isEmpty && Probe.results.last! == nil }
    }
}
'''

# A hosting view alone does not reproduce the empty-Group failure: exercise the
# production WindowGroup and openWindow route as well.
app_source = (root / 'Sources/Meridian.swift').read_text()
scene = app_source[app_source.index('        WindowGroup("Document"'):]
scene = scene[:scene.index('.commandsRemoved()') + len('.commandsRemoved()')]
scene_harness = r'''
import AppKit
import SwiftUI

enum Probe { static var results: [String?] = [] }
struct Trigger: View {
    @Environment(\.openWindow) private var openWindow
    var body: some View {
        Text("Document loading test").task {
            let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".md")
            defer { try? FileManager.default.removeItem(at: url) }
            try! "# Scene spec".write(to: url, atomically: true, encoding: .utf8)
            openWindow(id: "document", value: url)
            for _ in 0..<40 {
                if Probe.results.last == "# Scene spec" {
                    print("PASS: spec link loads content in a document window scene")
                    exit(0)
                }
                try? await Task.sleep(for: .milliseconds(100))
            }
            fputs("FAIL: document window never loaded the spec\n", stderr)
            exit(1)
        }
    }
}
@main struct DocumentSceneTest: App {
    var body: some Scene {
        Window("Loading regression test", id: "test") { Trigger() }
''' + scene + '\n    }\n}\n'

with tempfile.TemporaryDirectory(prefix='document-loading-') as directory:
    directory = Path(directory)
    view = directory / 'DocumentWindow.swift'
    main = directory / 'Harness.swift'
    binary = directory / 'document-test'
    view.write_text(source)
    for test in (scene_harness, harness):
        main.write_text(test)
        subprocess.run(['xcrun', 'swiftc', '-module-cache-path', str(directory / 'cache'),
                        '-parse-as-library', str(view), str(main), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True, timeout=30)
