import Foundation
import GhosttyTerminal
import Network

/// Connect the Ghostty surface directly to the persistent dtach server.
/// Closing a surface releases its socket without waiting for a PTY child to exit.
final class DtachConnection: @unchecked Sendable {
    private let queue = DispatchQueue(label: "local.muxteroid.dtach")
    private let socket: NWConnection
    private let streamID: String
    private weak var session: InMemoryTerminalSession?
    private var viewport: InMemoryTerminalViewport?
    private var attached = false
    private var finished = false

    init(path: String, streamID: String) {
        socket = NWConnection(to: .unix(path: path), using: .tcp)
        self.streamID = streamID
    }

    deinit { socket.cancel() }

    func start(restoreCodexMouseReporting: Bool) -> InMemoryTerminalSession {
        let session = InMemoryTerminalSession(write: { data in
            self.queue.async { self.sendInput(data) }
        }, resize: { size in
            self.queue.async {
                guard size.rows > 0, size.columns > 0 else { return }
                let firstSize = self.viewport == nil
                self.viewport = size
                if self.attached {
                    if firstSize { self.redraw() }
                    else { self.sendSize(size, redraw: false) }
                }
            }
        })
        // dtach streams new output only. A fresh Ghostty surface misses the
        // mouse modes Codex enabled at startup, so wheel events stay local.
        // Seed the surface before receiving output; the TUI can change modes later.
        if restoreCodexMouseReporting {
            session.receive("\u{1b}[?1000h\u{1b}[?1006h")
        }
        self.session = session
        socket.stateUpdateHandler = { [weak self] state in
            guard let self else { return }
            switch state {
            case .ready:
                self.attached = true
                WorkstreamLog.record("stream=\(self.streamID) dtach socket attached")
                self.send(Data([1] + Array(repeating: 0, count: 9)))
                self.redraw()
                self.receive()
            case .waiting, .failed:
                self.finish(code: 1)
            default: break
            }
        }
        socket.start(queue: queue)
        return session
    }

    private func redraw() {
        guard let size = viewport else { return }
        guard size.columns > 1 else { sendSize(size, redraw: true); return }
        // TUI clients may skip painting when attached at the previous grid size.
        var narrow = size
        narrow.columns -= 1
        sendSize(narrow, redraw: true)
        queue.asyncAfter(deadline: .now() + .milliseconds(150)) { [weak self] in
            if let self, let current = self.viewport { self.sendSize(current, redraw: true) }
        }
    }

    // dtach.h: type, length/redraw method, then eight bytes of input or a native winsize.
    private func sendSize(_ size: InMemoryTerminalViewport, redraw: Bool) {
        var packet = Data([redraw ? 4 : 3, redraw ? 3 : 0])
        var dimensions = winsize(ws_row: size.rows, ws_col: size.columns,
                                 ws_xpixel: UInt16(clamping: size.widthPixels),
                                 ws_ypixel: UInt16(clamping: size.heightPixels))
        withUnsafeBytes(of: &dimensions) { packet.append(contentsOf: $0) }
        send(packet)
    }

    private func sendInput(_ data: Data) {
        guard !data.isEmpty else { return }
        var packets = Data()
        for offset in stride(from: data.startIndex, to: data.endIndex, by: 8) {
            let end = min(offset + 8, data.endIndex)
            let count = end - offset
            packets.append(contentsOf: [0, UInt8(count)])
            packets.append(contentsOf: data[offset..<end])
            packets.append(contentsOf: repeatElement(0, count: 8 - count))
        }
        send(packets)
    }

    private func send(_ data: Data) {
        guard !finished else { return }
        socket.send(content: data, completion: .contentProcessed { [weak self] error in
            if error != nil { self?.finish(code: 1) }
        })
    }

    private func receive() {
        socket.receive(minimumIncompleteLength: 1, maximumLength: 32768) { [weak self] data, _, end, error in
            guard let self, !self.finished else { return }
            if let data { self.session?.receive(data) }
            if end || error != nil { self.finish(code: error == nil ? 0 : 1) }
            else { self.receive() }
        }
    }

    private func finish(code: UInt32) {
        guard !finished else { return }
        finished = true
        socket.cancel()
        WorkstreamLog.record("stream=\(streamID) dtach socket closed status=\(code)")
        session?.finish(exitCode: code, runtimeMilliseconds: 0)
    }
}
