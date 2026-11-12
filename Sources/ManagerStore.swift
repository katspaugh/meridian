import SwiftUI

struct ManagerAsk: Decodable, Identifiable { let id: Int; let time: Double; let stream, terminal, summary, recommendation: String; let options: [String] }
struct ManagerDecision: Decodable { let decision, why, undo: String }
struct ManagerEvent: Decodable, Identifiable { let id: Int; let time: Double; let stream, terminal, trigger, verdict, summary, message: String; let seen: Bool; let decisions: [ManagerDecision] }
struct ManagerChatMessage: Decodable, Identifiable { let id: Int; let time: Double; let role, text: String }
struct ManagerState: Decodable { let paused, briefingStale, sweepDue: Bool; let briefing: String; let briefingTime: Double?; let asks: [ManagerAsk]; let events: [ManagerEvent]; let chat: [ManagerChatMessage]; let unseen: Int; let messages: [ManagerMessage]; let unread: Int }

@MainActor final class ManagerStore: ObservableObject {
    @Published var state: ManagerState?
    @Published var sweeping = false
    private var refreshID = 0

    /// Returns whether a fresh state was loaded; on failure the previous state stays.
    @discardableResult func refresh() async -> Bool {
        refreshID += 1
        let id = refreshID
        guard let data = try? await Bridge.run(["manager-state"]),
              let loaded = try? JSONDecoder().decode(ManagerState.self, from: data),
              id == refreshID else { return false }
        state = loaded
        return true
    }

    func sweep(excluding terminal: String?) async {
        guard !sweeping else { return }
        sweeping = true
        defer { sweeping = false }
        _ = try? await Bridge.run(["manager-sweep"] + (terminal.map { [$0] } ?? []))
        await refresh()
    }

    func dismiss(_ ask: ManagerAsk) async { await run(["manager-dismiss", String(ask.id)]) }

    func act(_ message: ManagerMessage, _ action: String, index: Int? = nil) async -> Bool {
        let args = ["manager-message", String(message.id), String(message.revision), action] + (index.map { [String($0)] } ?? [])
        let data = try? await Bridge.run(args)
        await refresh()
        return data.flatMap { try? JSONDecoder().decode(ManagerActionResult.self, from: $0) }?.ok == true
    }
    func askAction(_ ask: ManagerAsk, _ action: String) async { await run(["manager-ask-action", String(ask.id), action]) }
    func readAsk(_ ask: ManagerAsk) async { await run(["manager-ask-seen", String(ask.id)]) }

    private func run(_ args: [String]) async {
        _ = try? await Bridge.run(args)
        await refresh()
    }
}

/// Full question details shown from the inspector.
struct ManagerAskDetails: View {
    let ask: ManagerAsk

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(ask.summary).fontWeight(.semibold)
            ForEach(Array(ask.options.enumerated()), id: \.offset) { _, option in
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text("•").foregroundStyle(.secondary)
                    Text(option)
                }
            }
            if !ask.recommendation.isEmpty {
                Text("Recommended: \(ask.recommendation)").foregroundStyle(.secondary)
            }
        }
        .fixedSize(horizontal: false, vertical: true)
        .textSelection(.enabled)
    }
}

struct ManagerActionResult: Decodable { let ok: Bool }
struct ManagerButton: Decodable { let label, action, terminal, value: String }
struct ManagerMessage: Decodable, Identifiable {
    let id, revision: Int
    let time: Double
    let stream, terminal, kind, text, rule: String
    let seen: Bool
    let buttons: [ManagerButton]
}

struct ManagerMessageCard: View {
    let message: ManagerMessage
    @ObservedObject var store: ManagerStore
    let terminals: [WorkstreamTerminal]
    let open: (String) -> Void
    var markWhenVisible = false
    @State private var saving: Int?
    @State private var acting = false
    @State private var failed = false

    private func disabled(_ button: ManagerButton) -> Bool {
        acting || (button.action == "tell" && !terminals.contains {
            $0.id == button.terminal && $0.live && ["Review", "Ready"].contains($0.state) && ["claude", "codex"].contains($0.provider)
        })
    }
    private func act(_ index: Int) {
        let button = message.buttons[index]
        Task {
            acting = true
            defer { acting = false }
            let ok = await store.act(message, button.action, index: index)
            failed = !ok
            if ok && button.action == "open" {
                if let url = URL(string: button.value), ["https", "http"].contains(url.scheme ?? "") {
                    NSWorkspace.shared.open(url)
                } else { open(button.terminal) }
            }
        }
    }
    private func markRead() {
        guard markWhenVisible, NSApp.isActive, !message.seen else { return }
        Task { _ = await store.act(message, "seen") }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(message.text).font(.callout).lineLimit(2).minimumScaleFactor(0.85)
                .foregroundStyle(message.kind == "status" ? Color.secondary : Color.primary)
                .frame(maxWidth: .infinity, minHeight: 34, alignment: .topLeading)
            HStack(spacing: 6) {
                ForEach(Array(message.buttons.enumerated()), id: \.offset) { index, button in
                    Button(button.label) {
                        if button.action == "save" { saving = index } else { act(index) }
                    }
                    .disabled(disabled(button))
                    .help(disabled(button) && button.action == "tell" ? "Agent is busy" : button.label)
                }
                if failed { Image(systemName: "exclamationmark.circle").help("Action could not run. Refresh and try again.") }
            }.controlSize(.small).frame(height: 22)
        }
        .popover(isPresented: Binding(get: { saving != nil }, set: { if !$0 { saving = nil } })) {
            VStack(alignment: .leading, spacing: 12) {
                Text("Save project rule?").font(.headline)
                ScrollView { Text(message.rule).textSelection(.enabled) }.frame(maxHeight: 240)
                HStack {
                    Button("Cancel") { saving = nil }
                    Button("Save") { if let index = saving { act(index) }; saving = nil }
                }
            }.padding(16).frame(width: 340)
        }
        .onAppear { markRead() }
        .onChange(of: message.revision) { _, _ in saving = nil; markRead() }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in markRead() }
    }
}

struct ManagerSlot: View {
    let stream: Stream
    let asks: [ManagerAsk]
    @ObservedObject var store: ManagerStore
    let openTerminal: (String) -> Void
    @State private var detailAsk: ManagerAsk?
    @State private var page = 0
    private var messages: [ManagerMessage] {
        let order = ["rule": 1, "reminder": 1, "next": 2, "status": 3]
        return (store.state?.messages ?? []).filter { $0.stream == stream.id }.sorted {
            (order[$0.kind] ?? 4, -$0.time) < (order[$1.kind] ?? 4, -$1.time)
        }
    }
    private var count: Int { asks.count + messages.count }
    private var index: Int { min(page, max(0, count - 1)) }
    private func readAsk() {
        guard NSApp.isActive, index < asks.count else { return }
        let ask = asks[index]
        Task { await store.readAsk(ask) }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Label("Meridian", systemImage: "sparkle").font(.caption).foregroundStyle(.secondary)
                Spacer()
                if count > 1 {
                    Button { page = (index + count - 1) % count } label: { Image(systemName: "chevron.left") }
                    Text("\(index + 1) of \(count)").font(.caption.monospacedDigit())
                    Button { page = (index + 1) % count } label: { Image(systemName: "chevron.right") }
                }
            }.buttonStyle(.plain).frame(height: 16)
            if index < asks.count {
                let ask = asks[index]
                VStack(alignment: .leading, spacing: 8) {
                    Text(String(ask.summary.prefix(100))).font(.callout).lineLimit(2).minimumScaleFactor(0.85)
                        .frame(maxWidth: .infinity, minHeight: 34, alignment: .topLeading)
                    HStack {
                        Button("Open") { Task { await store.askAction(ask, "open"); openTerminal(ask.terminal) } }
                        Button("Details") { Task { await store.askAction(ask, "details"); detailAsk = ask } }
                    }.controlSize(.small).frame(height: 22)
                }
            } else if index - asks.count < messages.count {
                ManagerMessageCard(message: messages[index - asks.count], store: store,
                                   terminals: stream.terminals, open: openTerminal, markWhenVisible: true)
                    .id(messages[index - asks.count].id)
            } else {
                Text("Nothing from Meridian").font(.callout).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            }
        }
        .padding(.horizontal, 16).padding(.vertical, 12)
        .frame(height: 120, alignment: .top).clipped().background(.bar)
        .popover(item: $detailAsk) { ask in
            VStack(alignment: .leading, spacing: 12) {
                ManagerAskDetails(ask: ask)
                Button("Dismiss") { Task { await store.dismiss(ask); detailAsk = nil } }
            }.padding(16).frame(width: 340)
        }
        .onChange(of: stream.id) { _, _ in page = 0; detailAsk = nil; readAsk() }
        .onChange(of: index) { _, _ in readAsk() }
        .onChange(of: asks.map(\.id)) { _, _ in readAsk() }
        .onAppear { readAsk() }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in readAsk() }
    }
}
