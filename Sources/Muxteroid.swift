import SwiftUI
import AppKit
import GhosttyTerminal

struct Subagent: Decodable, Identifiable {
    let id, title, role, state, revision: String
    let terminalID: String
}

struct WorkstreamTerminal: Decodable, Identifiable {
    let id, title, provider, state, detail: String
    let session, terminalSocket: String?
    let live, nativeView: Bool
    let agents: [Subagent]
}

struct BeadsTask: Decodable, Identifiable {
    let id, title, status: String
    let notes: String?
    let blockedReason, unblockAction: String?

    private func nonempty(_ value: String?) -> String? {
        let value = value?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return value.isEmpty ? nil : value
    }

    var blockerReason: String? {
        guard status == "blocked" else { return nil }
        return nonempty(blockedReason) ?? nonempty(notes) ?? "The agent has not recorded why this task is blocked."
    }

    var blockerAction: String? {
        guard status == "blocked" else { return nil }
        return nonempty(unblockAction) ?? "Ask the agent in this workstream what is missing and what you need to do to continue."
    }
}

struct WorkstreamLink: Decodable, Identifiable {
    let kind, label: String
    let url: URL
    var id: URL { url }
}

struct Stream: Decodable, Identifiable {
    let terminals: [WorkstreamTerminal]
    let agents: [Subagent]
    let tasks: [BeadsTask]
    let links: [WorkstreamLink]
    let pipeline: Pipeline?
    let documents: [WorkstreamDocument]
    let provider: String
    let session: String?
    let terminalSocket: String?
    let sourceFolder, worktreeBranch: String?
    let live, nativeView: Bool
    let id, title, folder, reference, state, detail, created: String

    var sidebarProviders: [String] {
        let providers = Set(terminals.filter { $0.live && $0.provider != "shell" }.map(\.provider))
        return providers.isEmpty ? [provider] : providers.sorted()
    }

    var needsAttention: Bool {
        state != "Done" && (state == "Needs you" || tasks.contains { $0.status == "blocked" })
    }
    var displayState: String { needsAttention ? "Needs you" : state }
}

extension Stream {
    private enum CodingKeys: String, CodingKey {
        case terminals, agents, tasks, links, pipeline, documents, provider, session, terminalSocket
        case sourceFolder, worktreeBranch, live, nativeView, id, title, folder, reference, state, detail, created
    }

    /// A pipeline or document list the app cannot read is dropped, so it never fails the stream or the list.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        terminals = try container.decode([WorkstreamTerminal].self, forKey: .terminals)
        agents = try container.decode([Subagent].self, forKey: .agents)
        tasks = try container.decode([BeadsTask].self, forKey: .tasks)
        links = try container.decode([WorkstreamLink].self, forKey: .links)
        pipeline = (try? container.decodeIfPresent(Pipeline.self, forKey: .pipeline)) ?? nil
        documents = (try? container.decode([WorkstreamDocument].self, forKey: .documents)) ?? []
        provider = try container.decode(String.self, forKey: .provider)
        session = try container.decodeIfPresent(String.self, forKey: .session)
        terminalSocket = try container.decodeIfPresent(String.self, forKey: .terminalSocket)
        sourceFolder = try container.decodeIfPresent(String.self, forKey: .sourceFolder)
        worktreeBranch = try container.decodeIfPresent(String.self, forKey: .worktreeBranch)
        live = try container.decode(Bool.self, forKey: .live)
        nativeView = try container.decode(Bool.self, forKey: .nativeView)
        id = try container.decode(String.self, forKey: .id)
        title = try container.decode(String.self, forKey: .title)
        folder = try container.decode(String.self, forKey: .folder)
        reference = try container.decode(String.self, forKey: .reference)
        state = try container.decode(String.self, forKey: .state)
        detail = try container.decode(String.self, forKey: .detail)
        created = try container.decode(String.self, forKey: .created)
    }
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
        case .attention: return stream.needsAttention
        case .review: return stream.state == "Review" && !stream.needsAttention
        case .done: return stream.state == "Done"
        }
    }
}

enum Bridge {
    static func run(_ args: [String], traceRefresh: Bool = false) async throws -> Data {
        try await Task.detached {
            let p = Process(), output = Pipe()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            p.arguments = [Bundle.main.url(forResource: "streams", withExtension: "py")!.path] + args
            let action = args[0]
            let traced = traceRefresh || ["create", "open", "view-agent"].contains(action)
            let stream = ["open", "view-agent"].contains(action) ? args[1] : "-"
            let context = "action=\(action) stream=\(stream)"
            if traced {
                WorkstreamLog.record("\(context) spawning bridge")
                p.environment = ProcessInfo.processInfo.environment.merging(["MUXTEROID_TRACE_REFRESH": "1"]) { _, new in new }
            }
            p.standardOutput = output
            let errors = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
            FileManager.default.createFile(atPath: errors.path, contents: nil)
            defer { try? FileManager.default.removeItem(at: errors) }
            let handle = try FileHandle(forWritingTo: errors)
            defer { try? handle.close() }
            p.standardError = handle
            do { try p.run() } catch {
                if traced { WorkstreamLog.record("\(context) bridge spawn failed") }
                throw error
            }
            if traced { WorkstreamLog.record("\(context) bridge started pid=\(p.processIdentifier)") }
            let data = output.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            if traced { WorkstreamLog.record("\(context) bridge exited pid=\(p.processIdentifier) status=\(p.terminationStatus)") }
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
    func refresh(traced: Bool = false) async {
        refreshID += 1
        let id = refreshID
        do {
            let data = try await Bridge.run(["list"], traceRefresh: traced)
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
            await refresh(traced: ["open", "create"].contains(args[0]))
            return String(decoding: result, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        } catch { self.error = error.localizedDescription; return nil }
    }
}

@MainActor
struct StreamTerminal: View {
    @State var disconnected = false
    let streamID: String
    let appearance: String
    var archive: String? = nil
    var focusRequest: UUID? = nil
    var codexChild = false
    var socket: String? = nil
    var isCodex = false
    var visible = true
    var onDisconnect: (() -> Void)? = nil
    var onRequestClose: (() -> Void)? = nil
    var sessionIdentity: String = ""

    var body: some View {
        Group {
        if disconnected {
            Button("Reconnect terminal") { disconnected = false }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            ConnectedTerminal(options: self) {
                disconnected = true
                onDisconnect?()
            }
        }
        }
        // A pre-upgrade agent can exit and be replaced by a shell at the same socket path.
        .onChange(of: sessionIdentity) { disconnected = false }
    }
}

@MainActor
private final class WorkstreamTerminalView: TerminalView {
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        let modifiers = event.modifierFlags.intersection([.command, .control, .option, .shift])
        if event.type == .keyDown, modifiers == .command,
           event.charactersIgnoringModifiers?.lowercased() == "q" {
            // Ghostty handles its own bindings before the app menu. Quit belongs
            // to the host and must never be forwarded to the terminal process.
            NSApp.terminate(nil)
            return true
        }
        return super.performKeyEquivalent(with: event)
    }
}

@MainActor
private struct ConnectedTerminal: View {
    @StateObject private var terminal: TerminalViewState
    let options: StreamTerminal
    let disconnected: () -> Void

    init(options: StreamTerminal, disconnected: @escaping () -> Void) {
        self.options = options
        self.disconnected = disconnected
        _terminal = StateObject(wrappedValue: Self.makeTerminal(options, disconnected: disconnected))
    }

    private static func makeTerminal(_ options: StreamTerminal, disconnected: @escaping () -> Void) -> TerminalViewState {
        let streamID = options.streamID
        WorkstreamLog.record("stream=\(streamID) terminal controller setup begin")
        let state = TerminalViewState(controller: TerminalController(configSource: .generated(options.appearance), theme: TerminalTheme()))
        state.makePlatformView = { WorkstreamTerminalView(frame: .zero) }
        if let socket = options.socket {
            let session = DtachConnection(path: socket, streamID: streamID, onDisconnect: disconnected)
                .start(restoreCodexMouseReporting: options.isCodex)
            state.configuration = TerminalSurfaceOptions(backend: .inMemory(session), waitAfterCommand: false)
            return state
        }
        let bridge = Bundle.main.url(forResource: "streams", withExtension: "py")!
        let arguments = options.archive.map { [options.codexChild ? "codex-child" : "agent-archive", streamID, $0] } ?? ["attach", streamID]
        let command = (["/usr/bin/python3", bridge.path] + arguments)
            .map { "'" + $0.replacingOccurrences(of: "'", with: "'\\''") + "'" }
            .joined(separator: " ")
        state.configuration = TerminalSurfaceOptions(command: command, waitAfterCommand: false)
        WorkstreamLog.record("stream=\(streamID) terminal configured action=\(arguments[0])")
        return state
    }

    var body: some View {
        TerminalSurfaceView(context: terminal)
            .onAppear {
                WorkstreamLog.record("stream=\(options.streamID) terminal surface appeared")
                terminal.onClose = { processAlive in
                    if processAlive, let close = options.onRequestClose { close() }
                    else { disconnected() }
                }
                terminal.isSurfaceVisible = options.visible
                if options.visible { terminal.requestFocus() }
            }
            .onChange(of: options.focusRequest) { if options.visible { terminal.requestFocus() } }
            .onChange(of: options.visible) {
                terminal.isSurfaceVisible = options.visible
                if options.visible { terminal.requestFocus() }
            }
            .onDisappear { terminal.onClose = nil }
    }
}

struct Dashboard: View {
    @ObservedObject var store: Store
    @ObservedObject var notifications: AttentionNotifications
    @State private var expanded: Set<Inbox> = [.active]
    @State private var adding = false
    @State private var showingBoard = false
    @State private var selection: String?
    @State private var agentSelection: String?
    @AppStorage("selectedWorkstreamTerminals") private var terminalSelections = "{}"
    @State private var archivedAgent: String?
    @State private var switching = false
    @State private var terminalFocus = UUID()
    @State private var expandedStreams: Set<String> = []
    @State private var appearance: String?
    @AppStorage("inspectorVisible") private var inspectorVisible = true
    @AppStorage("lastWorkstreamFolder") private var lastFolder = ""

    private var selected: Stream? { store.rows.first { $0.id == selection } }
    private func selectedTerminal(_ stream: Stream) -> WorkstreamTerminal? {
        let choices = (try? JSONDecoder().decode([String: String].self, from: Data(terminalSelections.utf8))) ?? [:]
        return stream.terminals.first { $0.id == choices[stream.id] } ?? stream.terminals.first
    }

    private func rememberTerminal(_ terminalID: String, in stream: Stream) {
        var choices = (try? JSONDecoder().decode([String: String].self, from: Data(terminalSelections.utf8))) ?? [:]
        choices[stream.id] = terminalID
        if let data = try? JSONEncoder().encode(choices) { terminalSelections = String(decoding: data, as: UTF8.self) }
    }
    private var suggestedFolder: String {
        ([lastFolder] + store.rows.sorted { $0.created > $1.created }.map { $0.sourceFolder ?? $0.folder })
            .first { FileManager.default.fileExists(atPath: $0) } ?? ""
    }

    private func sidebarRow(_ stream: Stream) -> some View {
        let providers = stream.sidebarProviders
        let agentName = providers.map { provider in
            switch provider {
            case "codex": return "Codex"
            case "claude": return "Claude"
            default: return "Terminal"
            }
        }.joined(separator: " + ")
        let summary = "\(stream.title) · \(agentName) · \(stream.displayState)"
        return Button {
            selectTerminal(stream, agent: nil)
        } label: {
            HStack {
                ZStack(alignment: .leading) {
                    ForEach(Array(providers.enumerated()), id: \.element) { index, provider in
                        AgentIcon(provider: provider)
                            .background(.background, in: Circle())
                            .offset(x: -CGFloat(index) * 6)
                            .zIndex(-Double(index))
                    }
                }
                .frame(width: 16, height: 16, alignment: .leading)
                Text(stream.title).lineLimit(2)
                Spacer(minLength: 0)
                Circle().fill(stateColor(stream.displayState)).frame(width: 7, height: 7)
            }
            .padding(.vertical, 3).contentShape(Rectangle())
        }
        .buttonStyle(.plain).disabled(switching)
        .listRowBackground(!showingBoard && selection == stream.id && agentSelection == nil ? Color.accentColor.opacity(0.16) : Color.clear)
        .accessibilityLabel(summary)
        .help(summary)
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
                    .listRowBackground(!showingBoard && selection == stream.id && agentSelection == agent.id ? Color.accentColor.opacity(0.16) : Color.clear)
                }
            } label: { sidebarRow(stream) }
        }
    }

    private func selectTerminal(_ stream: Stream, agent: Subagent?) {
        guard !switching else { return }
        if let agent { rememberTerminal(agent.terminalID, in: stream) }
        showingBoard = false
        terminalFocus = UUID()
        let previousStream = selection
        selection = stream.id
        archivedAgent = nil
        if previousStream != stream.id { agentSelection = nil }
        guard let terminal = selectedTerminal(stream) else { return }
        WorkstreamLog.record("stream=\(stream.id) terminal=\(terminal.id) select provider=\(terminal.provider) live=\(terminal.live) child=\(agent != nil)")
        if terminal.provider == "codex" {
            agentSelection = agent?.id
            archivedAgent = agent?.id
            return
        }
        if let agent, !terminal.nativeView || !terminal.live || stream.state == "Done" || !agent.id.hasPrefix((terminal.session ?? "") + "/") {
            archivedAgent = agent.id
            agentSelection = agent.id
            return
        }
        if agent == nil && (!terminal.live || !terminal.nativeView || terminal.agents.isEmpty || stream.state == "Done") {
            agentSelection = nil
            return
        }
        switching = true
        Task {
            defer { switching = false }
            do {
                let result = try await Bridge.run(["view-agent", terminal.id, agent?.id ?? ""])
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

    private func terminalBar(_ stream: Stream) -> some View {
        WorkstreamTerminalBar(terminals: stream.terminals, selectedID: selectedTerminal(stream)?.id ?? "",
                              switching: switching, canAdd: !store.busy && !switching && stream.state != "Done",
                              select: { terminal in
            rememberTerminal(terminal.id, in: stream)
            agentSelection = nil
            selectTerminal(stream, agent: nil)
        }, add: { provider in addTerminal(provider, to: stream) },
                              close: { terminal in closeTerminal(terminal, in: stream) })
    }

    private func closeTerminal(_ terminal: WorkstreamTerminal, in stream: Stream) {
        guard !store.busy, !switching else { return }
        Task {
            if await store.perform(["close-terminal", terminal.id]) != nil {
                agentSelection = nil
                archivedAgent = nil
                terminalFocus = UUID()
            }
        }
    }

    private func terminalDisconnected(_ terminal: WorkstreamTerminal) {
        Task {
            // Unlike user commands, transport cleanup must not be dropped while another action is busy.
            do {
                _ = try await Bridge.run(["terminal-exited", terminal.id])
                await store.refresh()
            } catch { store.error = error.localizedDescription }
        }
    }

    private func addTerminal(_ provider: String, to stream: Stream) {
        Task {
            if let id = await store.perform(["add-terminal", stream.id, provider]), !id.isEmpty {
                rememberTerminal(id, in: stream)
                if selection == stream.id {
                    agentSelection = nil
                    archivedAgent = nil
                    terminalFocus = UUID()
                }
            }
        }
    }

    private var sidebar: some View {
        List {
            Button { showingBoard = true } label: {
                Label("Sprint board", systemImage: "rectangle.split.3x1")
                    .frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
            }
            .buttonStyle(.plain).disabled(switching)
            .listRowBackground(showingBoard ? Color.accentColor.opacity(0.16) : Color.clear)
            .keyboardShortcut("b", modifiers: [.command, .shift])
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
                    if showingBoard {
                        SprintBoard(streams: store.rows, openStream: { stream in
                            selectTerminal(stream, agent: nil)
                            expanded.insert(stream.state == "Done" ? .done : .active)
                        }, newStream: { adding = true })
                    } else if let stream = selected {
                        let terminal = selectedTerminal(stream)
                        HSplitView {
                            VStack(spacing: 0) {
                                terminalBar(stream)
                                Divider()
                                ZStack {
                                if let appearance, stream.state != "Done" {
                                    ForEach(stream.terminals.filter(\.live)) { entry in
                                        let visible = entry.id == terminal?.id && archivedAgent == nil
                                        StreamTerminal(streamID: entry.id, appearance: appearance,
                                                       focusRequest: terminalFocus, socket: entry.terminalSocket,
                                                       isCodex: entry.provider == "codex", visible: visible,
                                                       onDisconnect: { terminalDisconnected(entry) },
                                                       onRequestClose: { closeTerminal(entry, in: stream) },
                                                       sessionIdentity: entry.session ?? entry.provider)
                                            .id(entry.id)
                                            .opacity(visible ? 1 : 0)
                                            .allowsHitTesting(visible)
                                            .accessibilityHidden(!visible)
                                    }
                                }
                                if let terminal {
                                if terminal.provider == "codex", agentSelection != nil, !terminal.live {
                                    Text("Start the workstream terminal to open this subagent.")
                                } else if let archivedAgent, archivedAgent == agentSelection, let appearance {
                                    StreamTerminal(streamID: terminal.id, appearance: appearance, archive: archivedAgent, codexChild: terminal.provider == "codex").id(archivedAgent)
                                } else if terminal.live && stream.state != "Done" {
                                    if appearance == nil { ProgressView() }
                                } else if stream.state == "Done" {
                                    Label("Completed", systemImage: "checkmark.circle")
                                } else {
                                    Button("Start terminal") { Task { _ = await store.perform(["open", terminal.id]) } }
                                        .buttonStyle(.borderedProminent).disabled(store.busy)
                                }
                                } else {
                                    Text("No terminals open. Use + to add one.").foregroundStyle(.secondary)
                                }
                                }.frame(maxWidth: .infinity, maxHeight: .infinity)
                            }.frame(minWidth: 260, maxWidth: .infinity, maxHeight: .infinity)
                            if inspectorVisible {
                                inspector(stream).id(stream.id).frame(minWidth: 218, idealWidth: 285, maxWidth: 410)
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
                .navigationTitle(showingBoard ? "Sprint board" : (selected?.title ?? "Workstreams"))
                .toolbar {
                    Button { adding = true } label: { Label("New workstream", systemImage: "plus") }.keyboardShortcut("n")
                    Button { inspectorVisible.toggle() } label: {
                        Label(inspectorVisible ? "Hide inspector" : "Show inspector", systemImage: "sidebar.right")
                    }
                    .disabled(showingBoard)
                    .help(inspectorVisible ? "Hide inspector" : "Show inspector")
                    .keyboardShortcut("i", modifiers: [.command, .option])
                }
            }
            Divider()
            UsageBar()
        }
        .frame(minWidth: 720, minHeight: 440)
        .focusedSceneValue(\.closeWorkstreamTerminal, closeTerminalAction)
        .task {
            WorkstreamLog.record("terminal appearance load begin")
            appearance = await GhosttyAppearance.load()
            WorkstreamLog.record("terminal appearance load finished")

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
                showingBoard = false
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

    private var closeTerminalAction: (() -> Void)? {
        guard !showingBoard, !store.busy, !switching,
              let stream = selected, let terminal = selectedTerminal(stream) else { return nil }
        return { closeTerminal(terminal, in: stream) }
    }

    private func selectNotification() {
        guard let id = notifications.destination,
              let stream = store.rows.first(where: { $0.id == id }) else { return }
        if let terminalID = notifications.terminalDestination {
            rememberTerminal(terminalID, in: stream)
        }
        selectTerminal(stream, agent: nil)
        expanded.insert(stream.state == "Done" ? .done : .active)
        notifications.destination = nil
        notifications.terminalDestination = nil
    }

    private func inspector(_ stream: Stream) -> some View {
        WorkstreamInspector(stream: stream, terminal: selectedTerminal(stream), busy: store.busy,
                            clearPipeline: { Task { _ = await store.perform(["pipeline-reset", stream.id]) } }) { keepTasksOpen in
            let reopening = stream.state == "Done"
            let args = [reopening ? "reopen" : "close", stream.id] + (keepTasksOpen && !reopening ? ["--force"] : [])
            Task {
                if await store.perform(args) != nil {
                    expanded.insert(stream.state == "Done" ? .active : .done)
                }
            }
        }
    }
}

private struct CloseWorkstreamTerminalKey: FocusedValueKey {
    typealias Value = () -> Void
}

extension FocusedValues {
    var closeWorkstreamTerminal: (() -> Void)? {
        get { self[CloseWorkstreamTerminalKey.self] }
        set { self[CloseWorkstreamTerminalKey.self] = newValue }
    }
}

struct TerminalCommands: Commands {
    @FocusedValue(\.closeWorkstreamTerminal) private var closeTerminal

    var body: some Commands {
        CommandGroup(replacing: .appTermination) {
            Button("Quit Muxteroid") { NSApp.terminate(nil) }
                .keyboardShortcut("q", modifiers: .command)
        }
        CommandGroup(after: .newItem) {
            Button("Close Terminal") { closeTerminal?() }
                .keyboardShortcut("w", modifiers: .command)
                .disabled(closeTerminal == nil)
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
            Text("Git repositories get a new branch and worktree shared by all terminals. Other folders are used directly.")
                .font(.caption).foregroundStyle(.secondary)
            TextField("Linear issue or URL (optional)", text: $reference)
            if !store.error.isEmpty { Text(store.error).foregroundStyle(.red) }
            HStack {
                Spacer()
                Button("Cancel") { store.error = ""; dismiss() }.keyboardShortcut(.cancelAction)
                Button("Create") {
                    Task {
                        if let id = await store.perform(["create", "", folder, reference, provider]) {
                            created(id, store.rows.first { $0.id == id }?.sourceFolder ?? folder)
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
        .commands { TerminalCommands() }
        .onChange(of: notifications.destination) { _, id in
            if id != nil { openWindow(id: "dashboard") }
        }
        WindowGroup("Document", id: "document", for: URL.self) { $url in
            if let url { DocumentWindow(url: url) }
        }.defaultSize(width: 760, height: 820)
        .commandsRemoved()
    }
}
