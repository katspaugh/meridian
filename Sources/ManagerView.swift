import SwiftUI
import MarkdownUI

func managerAge(_ time: Double) -> String {
    let seconds = max(0, Int(Date().timeIntervalSince1970 - time))
    switch seconds {
    case ..<60: return "just now"
    case ..<3600: return "\(seconds / 60) min ago"
    case ..<86_400: return "\(seconds / 3600) hr ago"
    default: return Date(timeIntervalSince1970: time).formatted(date: .abbreviated, time: .shortened)
    }
}

struct ManagerView: View {
    @ObservedObject var store: ManagerStore
    let streams: [Stream]
    let openStream: (Stream) -> Void
    let appearance: String?
    @State private var socket: String?
    @State private var terminalError: String?
    @State private var launchID = UUID()
    let newWorkstream: () -> Void
    @State private var showingOverview = false
    @State private var pendingPause: Bool?
    @State private var unread: Set<Int> = []

    private var state: ManagerState? { store.state }
    private func stream(_ id: String) -> Stream? { streams.first { $0.id == id } }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            if showingOverview {
                ScrollView {
                    VStack(alignment: .leading, spacing: 26) {
                        briefing
                        if let asks = state?.asks, !asks.isEmpty { needsYou(asks) }
                        ForEach(state?.commentErrors ?? [], id: \.self) { Text($0).foregroundStyle(.red) }
                        if let history = state?.chat, !history.isEmpty {
                            DisclosureGroup("Previous chat") {
                                ForEach(history) { message in
                                    VStack(alignment: .leading) {
                                        Text(message.role == "user" ? "You" : "Meridian").font(.caption.bold())
                                        Text(message.text).textSelection(.enabled)
                                    }.frame(maxWidth: .infinity, alignment: .leading)
                                }
                            }
                        }
                        activity
                    }
                    .frame(maxWidth: 760, alignment: .leading).padding(16)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }.frame(maxHeight: 280)
                Divider()
            }
            terminal

        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .task {
            if state == nil { await store.refresh() }
            // Opening the view is not a request for a consult; an out-of-date briefing waits ten minutes.
            guard let state, !state.paused, state.briefing.isEmpty
                    || (state.briefingStale && Date().timeIntervalSince1970 - (state.briefingTime ?? 0) > 600)
            else { return }
            await store.sweep(excluding: nil)
        }
        .task(id: "\(showingOverview)-\(state?.events.map(\.id).max() ?? 0)") {
            guard showingOverview else { return }
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
            .help("Pauses automatic reviews and follow-ups. Direct requests in this chat still work.")
            Spacer()
            Button(showingOverview ? "Hide overview" : "Overview (\(state?.asks.count ?? 0))") { showingOverview.toggle() }
            Button("New workstream…", action: newWorkstream)
            Button { Task { await store.sweep(excluding: nil) } } label: {
                if store.sweeping {
                    ProgressView().controlSize(.small)
                } else {
                    Image(systemName: "arrow.clockwise")
                }
            }
            .frame(width: 22, height: 18)
            .disabled(store.sweeping)
            .accessibilityLabel(store.sweeping ? "Refreshing the briefing" : "Refresh the briefing")
            .help("Ask Meridian to review every workstream and refresh the briefing")
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
                            LinearQuestionAction(stream: stream, ask: ask)
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
                Text("Nothing yet. Meridian records its actions here.").foregroundStyle(.secondary)
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

    private var terminal: some View {
        Group {
            if let socket, let appearance {
                StreamTerminal(streamID: "meridian-soul", appearance: appearance, socket: socket, onDisconnect: {
                    self.socket = nil
                    terminalError = "The Meridian terminal disconnected."
                })
            } else if let terminalError {
                VStack(spacing: 8) {
                    Text(terminalError).textSelection(.enabled)
                    Button("Reconnect to Claude Code") { launchID = UUID() }
                }
            } else {
                ProgressView("Opening Claude Code…")
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .task(id: launchID) {
            terminalError = nil
            do {
                let data = try await Bridge.run(["manager-terminal"])
                guard !Task.isCancelled else { return }
                socket = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            } catch {
                if !Task.isCancelled { terminalError = error.localizedDescription }
            }
        }
    }
}
