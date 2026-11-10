import SwiftUI
import AppKit
import MarkdownUI

/// A spec or plan from the Pipeline section, rendered, and reloaded when the file changes.
struct DocumentWindow: View {
    let url: URL

    @State private var text: String?
    @State private var checked = false

    var body: some View {
        Group {
            if let text {
                ScrollView {
                    Markdown(text)
                        .markdownTextStyle { FontSize(14) }
                        .textSelection(.enabled)
                        .frame(maxWidth: 760, alignment: .leading)
                        .padding(24)
                        .frame(maxWidth: .infinity)
                }
            } else if checked {
                ContentUnavailableView("Document not found", systemImage: "doc.questionmark", description: Text(url.path))
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .navigationTitle(url.lastPathComponent)
        .toolbar {
            Button("Open in Editor") { NSWorkspace.shared.open(url) }
            Button("Reveal in Finder") { NSWorkspace.shared.activateFileViewerSelecting([url]) }
        }
        // ⌘W belongs to the app's Close Terminal command, which is disabled without this value.
        .focusedSceneValue(\.closeWorkstreamTerminal, { NSApp.keyWindow?.performClose(nil) })
        .task(id: url) {
            var shown: Date?
            var loaded = false
            while !Task.isCancelled {
                let modified = (try? FileManager.default.attributesOfItem(atPath: url.path))?[.modificationDate] as? Date
                if !loaded || text == nil || modified != shown {
                    shown = modified
                    text = try? String(contentsOf: url, encoding: .utf8)
                    loaded = true
                    checked = true
                }
                do { try await Task.sleep(for: .seconds(2)) } catch { return }
            }
        }
    }
}
