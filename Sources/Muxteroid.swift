import SwiftUI
import AppKit
import GhosttyTerminal

struct Subagent: Decodable, Identifiable {
    let id, title, role, state, revision: String
}

struct BeadsTask: Decodable, Identifiable {
    let id, title, status: String
}

struct WorkstreamLink: Decodable, Identifiable {
    let kind, label: String
    let url: URL
    var id: URL { url }
}

struct Stream: Decodable, Identifiable {
    let agents: [Subagent]
    let tasks: [BeadsTask]
    let links: [WorkstreamLink]
    let provider: String
    let session: String?
    let live, nativeView: Bool
    let id, title, folder, reference, state, detail, created: String
}

func stateColor(_ state: String) -> Color {
    switch state {
    case "Needs you": return .orange
    case "Review": return .purple
    case "Working": return .blue
    case "Finished", "Done": return .green
    default: return .secondary
    }
}

enum Inbox: String, CaseIterable {
    case active = "Workstreams", attention = "Needs you", review = "Ready for review", done = "Completed"
    var icon: String {
        switch self {
        case .active: return "square.stack.3d.up"
        case .attention: return "bell"
        case .review: return "tray.full"
        case .done: return "checkmark.circle"
        }
    }
    func includes(_ stream: Stream) -> Bool {
        switch self {
        case .active: return stream.state != "Done"
        case .attention: return stream.state == "Needs you"
        case .review: return stream.state == "Review"
        case .done: return stream.state == "Done"
        }
    }
}

enum Bridge {
    static func run(_ args: [String]) async throws -> Data {
        try await Task.detached {
            let p = Process(), output = Pipe()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            p.arguments = [Bundle.main.url(forResource: "streams", withExtension: "py")!.path] + args
            p.standardOutput = output
            let errors = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
            FileManager.default.createFile(atPath: errors.path, contents: nil)
            defer { try? FileManager.default.removeItem(at: errors) }
            let handle = try FileHandle(forWritingTo: errors)
            defer { try? handle.close() }
            p.standardError = handle
            try p.run()
            let data = output.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            if p.terminationStatus != 0 {
                throw NSError(domain: "Muxteroid", code: Int(p.terminationStatus), userInfo: [
                    NSLocalizedDescriptionKey: (try? String(contentsOf: errors, encoding: .utf8)) ?? "Command failed."
                ])
            }
            return data
        }.value
    }
}

@MainActor final class Store: ObservableObject {
    @Published var rows = [Stream]()
    @Published var busy = false
    @Published var error = ""
    @Published var loadError = ""
    private var refreshID = 0
    func refresh() async {
        refreshID += 1
        let id = refreshID
        do {
            let data = try await Bridge.run(["list"])
            guard id == refreshID else { return }
            rows = try JSONDecoder().decode([Stream].self, from: data)
            loadError = ""
        } catch { if id == refreshID { loadError = error.localizedDescription } }
    }
    func perform(_ args: [String]) async -> String? {
        guard !busy else { return nil }
        busy = true
        defer { busy = false }
        do {
            let result = try await Bridge.run(args)
            await refresh()
            return String(decoding: result, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        } catch { self.error = error.localizedDescription; return nil }
    }
}

@MainActor
struct StreamTerminal: View {
    @StateObject private var terminal: TerminalViewState
    @State private var disconnected = false
    let focusRequest: UUID?

    init(streamID: String, appearance: String, archive: String? = nil, focusRequest: UUID? = nil, codexChild: Bool = false) {
        self.focusRequest = focusRequest
        _terminal = StateObject(wrappedValue: Self.makeTerminal(streamID, appearance, archive, codexChild))
    }

    private static func makeTerminal(_ streamID: String, _ appearance: String, _ archive: String?, _ codexChild: Bool) -> TerminalViewState {
        let state = TerminalViewState(controller: TerminalController(configSource: .generated(appearance), theme: TerminalTheme()))
        let bridge = Bundle.main.url(forResource: "streams", withExtension: "py")!
        let arguments = archive.map { [codexChild ? "codex-child" : "agent-archive", streamID, $0] } ?? ["attach", streamID]
        let command = (["/usr/bin/python3", bridge.path] + arguments)
            .map { "'" + $0.replacingOccurrences(of: "'", with: "'\\''") + "'" }
            .joined(separator: " ")
        state.configuration = TerminalSurfaceOptions(command: command, waitAfterCommand: false)
        return state
    }

    var body: some View {
        Group {
            if disconnected {
                Button("Reconnect terminal") { disconnected = false }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                TerminalSurfaceView(context: terminal)
                    .onAppear {
                        terminal.onClose = { _ in disconnected = true }
                        terminal.requestFocus()
                    }
                    .onChange(of: focusRequest) { terminal.requestFocus() }
                    .onDisappear { terminal.onClose = nil }
            }
        }
    }
}

struct Dashboard: View {
    @ObservedObject var store: Store
    @ObservedObject var notifications: AttentionNotifications
    @State private var expanded: Set<Inbox> = [.active]
    @State private var adding = false
    @State private var selection: String?
    @State private var agentSelection: String?
    @State private var archivedAgent: String?
    @State private var switching = false
    @State private var terminalFocus = UUID()
    @State private var expandedStreams: Set<String> = []
    @State private var appearance: String?
    @AppStorage("inspectorVisible") private var inspectorVisible = true
    @AppStorage("lastWorkstreamFolder") private var lastFolder = ""

    private var selected: Stream? { store.rows.first { $0.id == selection } }
    private var suggestedFolder: String {
        ([lastFolder] + store.rows.sorted { $0.created > $1.created }.map(\.folder))
            .first { FileManager.default.fileExists(atPath: $0) } ?? ""
    }

    private func sidebarRow(_ stream: Stream) -> some View {
        Button {
            selectTerminal(stream, agent: nil)
        } label: {
            HStack {
                Circle().fill(stateColor(stream.state)).frame(width: 7, height: 7)
                Text(stream.title).lineLimit(2)
                Spacer(minLength: 0)
            }
            .padding(.vertical, 3).contentShape(Rectangle())
        }
        .buttonStyle(.plain).disabled(switching)
        .listRowBackground(selection == stream.id && agentSelection == nil ? Color.accentColor.opacity(0.16) : Color.clear)
        .help(stream.title + " · " + stream.state)
    }

    @ViewBuilder private func streamBranch(_ stream: Stream) -> some View {
        if stream.agents.isEmpty {
            sidebarRow(stream)
        } else {
            DisclosureGroup(isExpanded: Binding(
                get: { expandedStreams.contains(stream.id) },
                set: { if $0 { expandedStreams.insert(stream.id) } else { expandedStreams.remove(stream.id) } }
            )) {
                ForEach(stream.agents) { agent in
                    Button {
                        selectTerminal(stream, agent: agent)
                    } label: {
                        HStack {
                            Image(systemName: "point.3.connected.trianglepath.dotted").foregroundStyle(stateColor(agent.state))
                            Text(agent.title).lineLimit(2)
                            Spacer(minLength: 0)
                        }.contentShape(Rectangle())
                    }
                    .buttonStyle(.plain).disabled(switching)
                    .help(agent.title + " · " + agent.state)
                    .listRowBackground(selection == stream.id && agentSelection == agent.id ? Color.accentColor.opacity(0.16) : Color.clear)
                }
            } label: { sidebarRow(stream) }
        }
    }

    private func selectTerminal(_ stream: Stream, agent: Subagent?) {
        guard !switching else { return }
        terminalFocus = UUID()
        let previousStream = selection
        selection = stream.id
        archivedAgent = nil
        if previousStream != stream.id { agentSelection = nil }
        if stream.provider == "codex" {
            agentSelection = agent?.id
            archivedAgent = agent?.id
            return
        }
        if let agent, !stream.nativeView || !stream.live || stream.state == "Done" || !agent.id.hasPrefix((stream.session ?? "") + "/") {
            archivedAgent = agent.id
            agentSelection = agent.id
            return
        }
        if agent == nil && (!stream.live || !stream.nativeView || stream.agents.isEmpty || stream.state == "Done") {
            agentSelection = nil
            return
        }
        switching = true
        Task {
            defer { switching = false }
            do {
                let result = try await Bridge.run(["view-agent", stream.id, agent?.id ?? ""])
                guard selection == stream.id else { return }
                agentSelection = agent?.id
                if String(decoding: result, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines) == "archive" {
                    archivedAgent = agent?.id
                }
            } catch {
                store.error = error.localizedDescription
            }
        }
    }

    private var sidebar: some View {
        List {
            ForEach(Inbox.allCases, id: \.self) { box in
                let streams = store.rows.filter(box.includes)
                DisclosureGroup(isExpanded: Binding(
                    get: { expanded.contains(box) },
                    set: { if $0 { expanded.insert(box) } else { expanded.remove(box) } }
                )) {
                    ForEach(streams) { stream in
                        streamBranch(stream)
                    }
                } label: {
                    Label(box.rawValue, systemImage: box.icon).badge(streams.count)
                }
            }
        }
        .navigationSplitViewColumnWidth(min: 175, ideal: 215, max: 320)
    }

    var body: some View {
        VStack(spacing: 0) {
            NavigationSplitView {
                sidebar
            } detail: {
                VStack(spacing: 0) {
                    if !store.loadError.isEmpty { Text(store.loadError).foregroundStyle(.red).padding(8) }
                    if let stream = selected {
                        HSplitView {
                            Group {
                                if stream.provider == "codex", agentSelection != nil, !stream.live {
                                    Text("Start the workstream terminal to open this subagent.")
                                } else if let archivedAgent, archivedAgent == agentSelection, let appearance {
                                    StreamTerminal(streamID: stream.id, appearance: appearance, archive: archivedAgent, codexChild: stream.provider == "codex").id(archivedAgent)
                                } else if stream.live && stream.state != "Done" {
                                    if let appearance {
                                        StreamTerminal(streamID: stream.id, appearance: appearance, focusRequest: terminalFocus).id(stream.id)
                                    } else { ProgressView() }
                                } else if stream.state == "Done" {
                                    Label("Completed", systemImage: "checkmark.circle")
                                } else {
                                    Button("Start terminal") { Task { _ = await store.perform(["open", stream.id]) } }
                                        .buttonStyle(.borderedProminent).disabled(store.busy)
                                }
                            }.frame(minWidth: 260, maxWidth: .infinity, maxHeight: .infinity)
                            if inspectorVisible {
                                inspector(stream).frame(minWidth: 218, idealWidth: 285, maxWidth: 410)
                            }
                        }
                        if !stream.links.isEmpty {
                            Divider()
                            ScrollView(.horizontal) {
                                HStack(spacing: 14) {
                                    ForEach(stream.links) { item in
                                        Link(destination: item.url) {
                                            Label(item.label, systemImage: item.kind == "pr" ? "arrow.triangle.pull" : "circle.hexagongrid")
                                        }
                                        .accessibilityLabel((item.kind == "pr" ? "Pull request " : "Linear issue ") + item.label)
                                        .help(item.url.absoluteString)
                                    }
                                }.font(.callout).padding(.horizontal, 12).padding(.vertical, 7)
                            }.frame(maxWidth: .infinity, alignment: .leading).background(.bar)
                        }
                    } else {
                        Button("New workstream…") { adding = true }
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                }
                .navigationTitle(selected?.title ?? "Workstreams")
                .toolbar {
                    Button { adding = true } label: { Label("New workstream", systemImage: "plus") }.keyboardShortcut("n")
                    Button { inspectorVisible.toggle() } label: {
                        Label(inspectorVisible ? "Hide inspector" : "Show inspector", systemImage: "sidebar.right")
                    }
                    .help(inspectorVisible ? "Hide inspector" : "Show inspector")
                    .keyboardShortcut("i", modifiers: [.command, .option])
                }
            }
            Divider()
            UsageBar()
        }
        .frame(minWidth: 720, minHeight: 440)
        .task {
            appearance = await GhosttyAppearance.load()

        }
        .onChange(of: notifications.destination, initial: true) { _, _ in selectNotification() }
        .onChange(of: store.rows.map(\.id)) { _, ids in
            selectNotification()
            if !ids.contains(selection ?? "") {
                selection = store.rows.first(where: Inbox.active.includes)?.id ?? ids.first
                if selected?.state == "Done" { expanded.insert(.done) }
            }
        }
        .sheet(isPresented: $adding) {
            NewStream(store: store, folder: suggestedFolder) { id, folder in
                lastFolder = folder
                selection = id
                agentSelection = nil
                expanded.insert(.active)
                adding = false
            }
        }
        .alert("Workstream error", isPresented: Binding(get: { !store.error.isEmpty && !adding }, set: { if !$0 { store.error = "" } })) {
            Button("OK") { store.error = "" }
        } message: { Text(store.error) }
    }

    private func selectNotification() {
        guard let id = notifications.destination,
              let stream = store.rows.first(where: { $0.id == id }) else { return }
        selectTerminal(stream, agent: nil)
        expanded.insert(stream.state == "Done" ? .done : .active)
        notifications.destination = nil
    }

    private func inspector(_ stream: Stream) -> some View {
        WorkstreamInspector(stream: stream, busy: store.busy) {
            Task {
                if await store.perform([stream.state == "Done" ? "reopen" : "close", stream.id]) != nil {
                    expanded.insert(stream.state == "Done" ? .active : .done)
                }
            }
        }
    }
}

struct NewStream: View {
    @ObservedObject var store: Store
    @Environment(\.dismiss) var dismiss
    @State var folder: String
    var created: (String, String) -> Void
    @State private var reference = ""
    @AppStorage("newStreamProvider") private var provider = "claude"
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("New workstream").font(.title2.bold())
            Picker("Agent", selection: $provider) {
                Text("Claude Code").tag("claude")
                Text("Codex CLI").tag("codex")
            }.pickerStyle(.segmented)
            HStack {
                TextField("Working directory", text: $folder)
                Button("Choose…") {
                    let panel = NSOpenPanel()
                    if !folder.isEmpty { panel.directoryURL = URL(fileURLWithPath: folder) }
                    panel.canChooseDirectories = true
                    panel.canChooseFiles = false
                    if panel.runModal() == .OK { folder = panel.url?.path ?? "" }
                }
            }
            TextField("Linear issue or URL (optional)", text: $reference)
            if !store.error.isEmpty { Text(store.error).foregroundStyle(.red) }
            HStack {
                Spacer()
                Button("Cancel") { store.error = ""; dismiss() }.keyboardShortcut(.cancelAction)
                Button("Create") {
                    Task {
                        if let id = await store.perform(["create", "", folder, reference, provider]) {
                            created(id, store.rows.first { $0.id == id }?.folder ?? folder)
                            _ = await store.perform(["open", id])
                        }
                    }
                }.keyboardShortcut(.defaultAction).disabled(folder.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }.disabled(store.busy)
        }.padding(24).frame(width: 450)
    }
}

@main struct MuxteroidApp: App {
    @NSApplicationDelegateAdaptor(AttentionNotifications.self) private var notifications
    @Environment(\.openWindow) private var openWindow
    var body: some Scene {
        Window("Muxteroid", id: "dashboard") {
            Dashboard(store: notifications.store, notifications: notifications)
        }.defaultSize(width: 1080, height: 760)
        .onChange(of: notifications.destination) { _, id in
            if id != nil { openWindow(id: "dashboard") }
        }
    }
}
