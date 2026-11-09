import AppKit
import GhosttyTerminal

enum GhosttyAppearance {
    /// Ghostty resolves theme names, includes and the macOS config search order.
    static func load() async -> String {
        let app = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.mitchellh.ghostty")
        guard let executable = app.flatMap({ Bundle(url: $0)?.executableURL }) else { return "" }
        return await Task.detached {
            let process = Process(), output = Pipe()
            process.executableURL = executable
            process.arguments = ["+show-config", "--no-pager"]
            process.standardOutput = output
            process.standardError = FileHandle.nullDevice
            do { try process.run() } catch { return "" }
            let data = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            guard process.terminationStatus == 0 else { return "" }
            let exact = ["foreground", "background", "palette", "bold-color", "faint-opacity", "minimum-contrast", "macos-option-as-alt", "term"]
            let prefixes = ["font-", "adjust-", "cursor-", "selection-", "window-padding-", "custom-shader", "background-", "bold-", "alpha-blending"]
            return String(decoding: data, as: UTF8.self).split(separator: "\n").filter { line in
                let key = line.prefix { $0 != "=" }.trimmingCharacters(in: .whitespaces)
                return exact.contains(key) || prefixes.contains(where: key.hasPrefix)
            }.joined(separator: "\n")
        }.value
    }
}
