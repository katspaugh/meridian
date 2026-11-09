import Foundation
import Darwin

enum WorkstreamLog {
    private static let queue = DispatchQueue(label: "local.muxteroid.workstream-log", qos: .utility)
    private static let directory = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/Muxteroid", isDirectory: true)
    private static let formatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()
    // Accessed only on queue.
    private static var lastDay = ""

    static func record(_ message: String) {
        let now = Date()
        queue.async {
            let timestamp = formatter.string(from: now)
            let day = String(timestamp.prefix(10))
            if day != lastDay {
                let files = FileManager.default
                do {
                    try files.createDirectory(at: directory, withIntermediateDirectories: true,
                                              attributes: [.posixPermissions: 0o700])
                } catch { return }
                let cutoff = String(formatter.string(from: now.addingTimeInterval(-6 * 86400)).prefix(10))
                for name in (try? files.contentsOfDirectory(atPath: directory.path)) ?? [] {
                    if name.range(of: #"^workstream-\d{4}-\d{2}-\d{2}\.log$"#, options: .regularExpression) != nil,
                       name < "workstream-\(cutoff).log" {
                        try? files.removeItem(at: directory.appendingPathComponent(name))
                    }
                }
                lastDay = day
            }
            let path = directory.appendingPathComponent("workstream-\(day).log").path
            let fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0o600)
            guard fd >= 0 else { return }
            defer { close(fd) }
            let line = "\(timestamp) app[\(getpid())] \(message)\n"
            line.utf8CString.withUnsafeBufferPointer { buffer in
                _ = Darwin.write(fd, buffer.baseAddress, buffer.count - 1)
            }
        }
    }
}
