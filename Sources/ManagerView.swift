import SwiftUI
import MarkdownUI

struct ManagerAsk: Decodable, Identifiable { let id: Int; let time: Double; let stream, terminal, summary, recommendation: String; let options: [String] }
struct ManagerDecision: Decodable { let decision, why, undo: String }
struct ManagerEvent: Decodable, Identifiable { let id: Int; let time: Double; let stream, terminal, trigger, verdict, summary, message: String; let seen: Bool; let decisions: [ManagerDecision] }
struct ManagerChatMessage: Decodable, Identifiable { let id: Int; let time: Double; let role, text: String }
struct ManagerState: Decodable { let paused, briefingStale, sweepDue: Bool; let briefing: String; let briefingTime: Double?; let asks: [ManagerAsk]; let events: [ManagerEvent]; let chat: [ManagerChatMessage]; let unseen: Int }

@MainActor final class ManagerStore: ObservableObject {
    @Published var state: ManagerState?
    @Published var sweeping = false
    @Published var sending = false
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

    func send(_ text: String) async {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !sending, !text.isEmpty else { return }
        sending = true
        defer { sending = false }
        _ = try? await Bridge.run(["manager-chat", text])
        await refresh()
    }

    func setPaused(_ paused: Bool) async { await run(["manager-pause", paused ? "1" : "0"]) }
    func dismiss(_ ask: ManagerAsk) async { await run(["manager-dismiss", String(ask.id)]) }
    func markSeen() async { await run(["manager-seen"]) }

    private func run(_ args: [String]) async {
        _ = try? await Bridge.run(args)
        await refresh()
    }
}

func managerAge(_ time: Double) -> String {
    let seconds = max(0, Int(Date().timeIntervalSince1970 - time))
    switch seconds {
    case ..<60: return "just now"
    case ..<3600: return "\(seconds / 60) min ago"
    case ..<86_400: return "\(seconds / 3600) hr ago"
    default: return Date(timeIntervalSince1970: time).formatted(date: .abbreviated, time: .shortened)
    }
}

/// The question itself, shared by the Manager view and the inspector.
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

struct ManagerView: View {
    @ObservedObject var store: ManagerStore
    let streams: [Stream]
    let openStream: (Stream) -> Void
    let focusedTerminal: String?
    @State private var draft = ""
    @State private var pendingPause: Bool?
    @State private var unread: Set<Int> = []

    private var state: ManagerState? { store.state }
    private func stream(_ id: String) -> Stream? { streams.first { $0.id == id } }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            VSplitView {
                ScrollView {
                    VStack(alignment: .leading, spacing: 26) {
                        briefing
                        if let asks = state?.asks, !asks.isEmpty { needsYou(asks) }
                        activity
                    }
                    .frame(maxWidth: 760, alignment: .leading)
                    .padding(16)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(minHeight: 120, maxHeight: .infinity)
                chat.frame(minHeight: 110, idealHeight: 220, maxHeight: .infinity)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .task {
            if state == nil { await store.refresh() }
            guard let state, !state.paused, state.briefing.isEmpty || state.briefingStale else { return }
            await store.sweep(excluding: focusedTerminal)
        }
        .task(id: state?.events.map(\.id).max()) {
            unread.formUnion(state?.events.filter { !$0.seen }.map(\.id) ?? [])
            try? await Task.sleep(for: .seconds(2))
            guard !Task.isCancelled, (state?.unseen ?? 0) > 0 else { return }
            await store.markSeen()
        }
    }

    private var header: some View {
        HStack(spacing: 12) {
            Toggle("Paused", isOn: Binding(get: { pendingPause ?? state?.paused ?? false }, set: { paused in
                pendingPause = paused
                Task {
                    await store.setPaused(paused)
                    pendingPause = nil
                }
            }))
            .toggleStyle(.switch).controlSize(.small).disabled(state == nil)
            .help("While paused, agents stop and ask you as they did before")
            Spacer()
            Button { Task { await store.sweep(excluding: focusedTerminal) } } label: {
                if store.sweeping {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: "arrow.clockwise")
                }
            }
            .frame(width: 22, height: 18)
            .disabled(store.sweeping)
            .accessibilityLabel(store.sweeping ? "Refreshing the briefing" : "Refresh the briefing")
            .help("Ask the manager to review every workstream and refresh the briefing")
        }
        .padding(.horizontal, 16).padding(.vertical, 9)
        .background(.bar)
    }

    private func sectionTitle(_ title: String) -> some View {
        Text(title).font(.title3.bold())
    }

    private var briefing: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                sectionTitle("Briefing")
                Spacer(minLength: 8)
                if let time = state?.briefingTime, !(state?.briefing.isEmpty ?? true) {
                    Text("Updated \(managerAge(time))").font(.caption).foregroundStyle(.secondary)
                    if state?.briefingStale == true {
                        Text("Out of date").font(.caption.weight(.semibold)).foregroundStyle(.orange)
                    }
                }
            }
            if let text = state?.briefing, !text.isEmpty {
                Markdown(text)
                    .markdownTextStyle { FontSize(13) }
                    .markdownImageProvider(.asset)
                    .markdownInlineImageProvider(.asset)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                Text("No briefing yet. Refresh to get one.").foregroundStyle(.secondary)
            }
        }
    }

    private func streamLabel(_ id: String) -> some View {
        let stream = stream(id)
        return HStack(spacing: 5) {
            if let stream { Circle().fill(stateColor(stream.displayState)).frame(width: 6, height: 6) }
            Text(stream?.title ?? id).lineLimit(1)
        }
    }

    private func needsYou(_ asks: [ManagerAsk]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            sectionTitle("Needs you")
            ForEach(asks) { ask in
                VStack(alignment: .leading, spacing: 10) {
                    streamLabel(ask.stream).font(.caption).foregroundStyle(.secondary)
                    ManagerAskDetails(ask: ask).font(.callout)
                    HStack(spacing: 8) {
                        if let stream = stream(ask.stream) {
                            Button("Open workstream") { openStream(stream) }
                        }
                        Button("Dismiss") { Task { await store.dismiss(ask) } }
                    }.controlSize(.small)
                }
                .frame(maxWidth: .infinity, alignment: .leading).padding(12)
                .background(Color.orange.opacity(0.07), in: RoundedRectangle(cornerRadius: 8))
                .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color.orange.opacity(0.22)))
            }
        }
    }

    private var activity: some View {
        VStack(alignment: .leading, spacing: 12) {
            sectionTitle("Decisions and activity")
            let events = (state?.events ?? []).sorted { ($0.time, $0.id) > ($1.time, $1.id) }
            if events.isEmpty {
                Text("Nothing yet. The manager records everything it does here.").foregroundStyle(.secondary)
            }
            LazyVStack(alignment: .leading, spacing: 12) {
                ForEach(events) { event in
                    if event.id != events.first?.id { Divider() }
                    eventRow(event)
                }
            }
        }
    }

    private func eventRow(_ event: ManagerEvent) -> some View {
        let isUnread = !event.seen || unread.contains(event.id)
        return HStack(alignment: .firstTextBaseline, spacing: 8) {
            Circle().fill(isUnread ? Color.accentColor : Color.clear).frame(width: 7, height: 7)
                .accessibilityLabel("Unread").accessibilityHidden(!isUnread)
            VStack(alignment: .leading, spacing: 6) {
                Text(event.summary).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                HStack(spacing: 5) {
                    if !event.stream.isEmpty {
                        streamLabel(event.stream)
                        Text("·")
                    }
                    Text(managerAge(event.time)).fixedSize()
                }.font(.caption).foregroundStyle(.secondary)
                if !event.message.isEmpty {
                    Text("Told the agent: \(event.message)").font(.callout)
                        .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                }
                ForEach(Array(event.decisions.enumerated()), id: \.offset) { _, decision in
                    VStack(alignment: .leading, spacing: 3) {
                        Text(decision.decision).fontWeight(.semibold)
                        if !decision.why.isEmpty { Text(decision.why).foregroundStyle(.secondary) }
                        if !decision.undo.isEmpty { Text("To undo: \(decision.undo)") }
                    }
                    .font(.callout).padding(.leading, 10)
                    .overlay(alignment: .leading) { Rectangle().fill(.quaternary).frame(width: 2) }
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                }
            }
        }
        .accessibilityElement(children: .combine)
    }

    private var chat: some View {
        let messages = state?.chat ?? []
        return VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 10) {
                        sectionTitle("Chat")
                        ForEach(messages) { message in chatMessage(message).id(message.id) }
                        if store.sending {
                            HStack(spacing: 6) {
                                ProgressView().controlSize(.small)
                                Text("The manager is replying…").foregroundStyle(.secondary)
                            }.id(-1)
                        }
                    }.padding(16)
                }
                .defaultScrollAnchor(.bottom)
                .onChange(of: messages.last?.id) { _, id in
                    if let id { withAnimation { proxy.scrollTo(id, anchor: .bottom) } }
                }
                .onChange(of: store.sending) { _, sending in if sending { withAnimation { proxy.scrollTo(-1, anchor: .bottom) } } }
            }
            Divider()
            HStack(alignment: .bottom, spacing: 8) {
                TextField("Message the manager", text: $draft, axis: .vertical)
                    .lineLimit(1...5).textFieldStyle(.roundedBorder)
                    .onSubmit(send)
                    .accessibilityLabel("Message the manager")
                Button("Send", action: send)
                    .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            .disabled(store.sending)
            .padding(.horizontal, 16).padding(.vertical, 10)
            .background(.bar)
        }
    }

    @ViewBuilder private func chatMessage(_ message: ManagerChatMessage) -> some View {
        switch message.role {
        case "user":
            HStack {
                Spacer(minLength: 48)
                Text(message.text)
                    .padding(.horizontal, 10).padding(.vertical, 7)
                    .background(Color.accentColor.opacity(0.15), in: RoundedRectangle(cornerRadius: 10))
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
        case "error":
            Label(message.text, systemImage: "exclamationmark.triangle")
                .foregroundStyle(.red).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
        default:
            Markdown(message.text)
                .markdownTextStyle { FontSize(13) }
                .markdownImageProvider(.asset)
                .markdownInlineImageProvider(.asset)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.trailing, 48)
        }
    }

    private func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !store.sending, !text.isEmpty else { return }
        draft = ""
        Task { await store.send(text) }
    }
}
