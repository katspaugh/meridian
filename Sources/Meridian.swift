import SwiftUI
import AppKit
import GhosttyTerminal

struct Subagent: Decodable, Identifiable {
    let id, title, role, state, revision: String
    let terminalID: String
}

struct WorkstreamTerminal: Decodable, Identifiable {
    let id, title, provider, state, detail: String
    var nowText: String? = nil
    var nowStarted: Double? = nil
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
    let primary: LinearIssue?
    let linearQuestions: [LinearQuestion]?
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
        case primary, linearQuestions
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
        primary = try container.decodeIfPresent(LinearIssue.self, forKey: .primary)
        linearQuestions = try container.decodeIfPresent([LinearQuestion].self, forKey: .linearQuestions)
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
    static func run(_ args: [String], traceRefresh: Bool = false, input: Data? = nil) async throws -> Data {
        try await Task.detached {
            let p = Process(), output = Pipe(), stdin = Pipe()
            p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            p.arguments = [Bundle.main.url(forResource: "streams", withExtension: "py")!.path] + args
            let action = args[0]
            let traced = traceRefresh || ["create", "open", "view-agent"].contains(action)
            let stream = ["open", "view-agent"].contains(action) ? args[1] : "-"
            let context = "action=\(action) stream=\(stream)"
            // The app may have been relaunched from an agent's shell; its bridge calls act for the owner.
            var environment = ProcessInfo.processInfo.environment
            environment["MERIDIAN_TERMINAL"] = nil
            environment["MERIDIAN_MANAGER"] = nil
            if traced {
                WorkstreamLog.record("\(context) spawning bridge")
                environment["MERIDIAN_TRACE_REFRESH"] = "1"
            }
            p.environment = environment
            p.standardOutput = output
            if input != nil { p.standardInput = stdin }
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
            if let input {
                stdin.fileHandleForWriting.write(input)
                try? stdin.fileHandleForWriting.close()
            }
            if traced { WorkstreamLog.record("\(context) bridge started pid=\(p.processIdentifier)") }
            let data = output.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            if traced { WorkstreamLog.record("\(context) bridge exited pid=\(p.processIdentifier) status=\(p.terminationStatus)") }
            if p.terminationStatus != 0 {
                throw NSError(domain: "Meridian", code: Int(p.terminationStatus), userInfo: [
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
    @Published var pulls = [String: PullRequest]()
    private var refreshID = 0
    /// Merged pull requests no longer change, so only the others are looked up again.
    func refreshPulls(_ urls: [URL]) async {
        let pending = urls.map(\.absoluteString).filter { pulls[$0]?.state != "MERGED" }
        guard !pending.isEmpty, let data = try? await Bridge.run(["pulls"] + pending),
              let found = try? JSONDecoder().decode([String: PullRequest].self, from: data) else { return }
        guard !Task.isCancelled else { return }
        for url in pending where found[url] == nil { pulls.removeValue(forKey: url) }
        pulls.merge(found) { _, new in new }
    }
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
    /// Lets the bridge restart an idle agent whose session cannot show colors before the pane connects.
    var refreshBeforeConnect = false
    @State private var refreshed = false

    var body: some View {
        Group {
        if disconnected {
            Button("Reconnect terminal") { disconnected = false }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if refreshBeforeConnect, socket != nil, !refreshed {
            Color.clear.task {
                // Connect regardless: a failed check leaves the session as it was.
                _ = try? await Bridge.run(["refresh-terminal", streamID])
                refreshed = true
            }
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
    @ObservedObject var manager: ManagerStore
    private enum Pane { case workstream, board, meridian }
    @State private var expanded: Set<Inbox> = [.active]
    @State private var workstreamQuery = ""
    @State private var adding = false
    @State private var pane = Pane.meridian
    @State private var selection: String?
    @State private var agentSelection: String?
    @AppStorage("selectedWorkstreamTerminals") private var terminalSelections = "{}"
    @State private var archivedAgent: String?
    @State private var switching = false
    @State private var terminalFocus = UUID()
    @State private var expandedStreams: Set<String> = []
    @State private var blockerDestination: String?
    @State private var appearance: String?
    @AppStorage("inspectorVisible") private var inspectorVisible = true
    @AppStorage("lastWorkstreamFolder") private var lastFolder = ""
    @AppStorage("recentWorkstreamFolders") private var recentFoldersJSON = "[]"

    private var selected: Stream? { store.rows.first { $0.id == selection } }
    private var selectedTerminalID: String? { selected.flatMap(selectedTerminal)?.id }
    private func selectedTerminal(_ stream: Stream) -> WorkstreamTerminal? {
        let choices = (try? JSONDecoder().decode([String: String].self, from: Data(terminalSelections.utf8))) ?? [:]
        return stream.terminals.first { $0.id == choices[stream.id] } ?? stream.terminals.first
    }

    private func rememberTerminal(_ terminalID: String, in stream: Stream) {
        var choices = (try? JSONDecoder().decode([String: String].self, from: Data(terminalSelections.utf8))) ?? [:]
        choices[stream.id] = terminalID
        if let data = try? JSONEncoder().encode(choices) { terminalSelections = String(decoding: data, as: UTF8.self) }
    }
    private var recentFolders: [String] {
        let saved = (try? JSONDecoder().decode([String].self, from: Data(recentFoldersJSON.utf8))) ?? []
        let candidates = [lastFolder] + saved + store.rows.sorted { $0.created > $1.created }.map { $0.sourceFolder ?? $0.folder }
        var seen = Set<String>()
        return Array(candidates.compactMap { candidate -> String? in
            guard !candidate.isEmpty else { return nil }
            let path = URL(fileURLWithPath: (candidate as NSString).expandingTildeInPath).resolvingSymlinksInPath().standardizedFileURL.path
            var isDirectory: ObjCBool = false
            guard FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory),
                  isDirectory.boolValue, seen.insert(path).inserted else { return nil }
            return path
        }.prefix(8))
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
        return VStack(alignment: .leading, spacing: 2) {
            Button {
                selectTerminal(stream, agent: nil)
            } label: {
                HStack {
                    ZStack(alignment: .leading) {
                        ForEach(Array(providers.enumerated()), id: \.element) { index, provider in
                            AgentIcon(provider: provider)
                                .mask {
                                    Rectangle()
                                        .overlay {
                                            if index > 0 {
                                                // Clear the area behind the preceding icon without
                                                // painting over the sidebar or selection background.
                                                Circle()
                                                    .offset(x: 6)
                                                    .blendMode(.destinationOut)
                                            }
                                        }
                                        .compositingGroup()
                                }
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
            .accessibilityLabel(summary)
            .help(summary)
            if let cue = stream.nextCue {
                Button { follow(cue, in: stream) } label: {
                    Label {
                        Text(cue.title).lineLimit(1)
                    } icon: {
                        Image(systemName: cue.symbol).foregroundStyle(cue.color)
                    }
                    .font(.caption.weight(.medium))
                }
                .buttonStyle(.plain).disabled(switching)
                .help(cue.help)
                .accessibilityLabel(stream.title + ", " + cue.title)
                .accessibilityHint(cue.help)
            }
        }
        .listRowBackground(pane == .workstream && selection == stream.id && agentSelection == nil ? Color.accentColor.opacity(0.16) : Color.clear)
    }

    private func follow(_ cue: WorkstreamCue, in stream: Stream) {
        switch cue {
        case let .input(terminal, agent):
            rememberTerminal(terminal.id, in: stream)
            if agent != nil { expandedStreams.insert(stream.id) }
            selectTerminal(stream, agent: agent)
        case .blocked, .complete:
            inspectorVisible = true
            if case .blocked = cue { blockerDestination = stream.id }
            selectTerminal(stream, agent: nil)
        }
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
                    .listRowBackground(pane == .workstream && selection == stream.id && agentSelection == agent.id ? Color.accentColor.opacity(0.16) : Color.clear)
                }
            } label: { sidebarRow(stream) }
        }
    }

    private func selectTerminal(_ stream: Stream, agent: Subagent?) {
        guard !switching else { return }
        if let agent { rememberTerminal(agent.terminalID, in: stream) }
        pane = .workstream
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

    @ViewBuilder private var workstreamList: some View {
        let query = workstreamQuery.trimmingCharacters(in: .whitespacesAndNewlines)
        let matches = query.isEmpty ? store.rows : store.rows.filter {
            $0.title.localizedStandardContains(query) || $0.id.localizedStandardContains(query)
        }
        ForEach(Inbox.allCases, id: \.self) { box in
            let streams = matches.filter(box.includes)
            DisclosureGroup(isExpanded: Binding(
                get: { expanded.contains(box) },
                set: { if $0 { expanded.insert(box) } else { expanded.remove(box) } }
            )) {
                ForEach(streams) { stream in streamBranch(stream) }
            } label: {
                Label(box.rawValue, systemImage: box.icon).badge(streams.count)
            }
        }
        if !query.isEmpty && matches.isEmpty {
            Text("No matching workstreams").foregroundStyle(.secondary)
        }
    }

    private var sidebar: some View {
        VStack(spacing: 0) {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                    .accessibilityHidden(true)
                TextField("Search workstreams", text: $workstreamQuery)
                    .textFieldStyle(.plain)
                    .onExitCommand { workstreamQuery = "" }
                if !workstreamQuery.isEmpty {
                    Button { workstreamQuery = "" } label: {
                        Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Clear search")
                }
            }
            .padding(6)
            .background(.quaternary, in: RoundedRectangle(cornerRadius: 6))
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            List {
                Button { pane = .meridian } label: {
                    Label("Meridian", systemImage: "sparkle")
                        .frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                }
                .buttonStyle(.plain).disabled(switching)
                .badge(manager.state?.asks.count ?? 0)
                .listRowBackground(pane == .meridian ? Color.accentColor.opacity(0.16) : Color.clear)
                .keyboardShortcut("m", modifiers: [.command, .shift])
                Button { pane = .board } label: {
                    Label("Sprint board", systemImage: "rectangle.split.3x1")
                        .frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                }
                .buttonStyle(.plain).disabled(switching)
                .listRowBackground(pane == .board ? Color.accentColor.opacity(0.16) : Color.clear)
                .keyboardShortcut("b", modifiers: [.command, .shift])
                workstreamList
            }
            if pane == .workstream, let stream = selected {
                Divider()
                ManagerSlot(stream: stream,
                            asks: manager.state?.asks.filter { $0.stream == stream.id } ?? [],
                            store: manager, openTerminal: { id in
                    if !id.isEmpty { rememberTerminal(id, in: stream) }
                    openFromOverview(stream)
                })
                .id(stream.id)
            }
        }
        .navigationSplitViewColumnWidth(min: 175, ideal: 215, max: 320)
    }

    private var board: some View {
        let urls = Set(store.rows.flatMap { $0.links.filter { $0.kind == "pr" }.map(\.url) })
            .sorted { $0.absoluteString < $1.absoluteString }
        return SprintBoard(streams: store.rows, openStream: openFromOverview, newStream: { adding = true },
                           pullAttention: { stream in
            stream.links.flatMap { link in
                (store.pulls[link.url.absoluteString]?.attention ?? []).map { link.label + " · " + $0 }
            }
        })
        .task(id: urls) {
            while !Task.isCancelled {
                await store.refreshPulls(urls)
                do { try await Task.sleep(for: .seconds(60)) } catch { return }
            }
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            NavigationSplitView {
                sidebar
            } detail: {
                VStack(spacing: 0) {
                    if !store.loadError.isEmpty { Text(store.loadError).foregroundStyle(.red).padding(8) }
                    if pane == .meridian {
                        ManagerView(store: manager, streams: store.rows, openStream: openFromOverview,
                                    appearance: appearance, newWorkstream: { adding = true })
                    } else if pane == .board {
                        board
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
                                                       sessionIdentity: entry.session ?? entry.provider,
                                                       refreshBeforeConnect: true)
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
                        // Pull requests are listed under Changes in the inspector.
                        let issues = stream.links.filter { $0.kind == "github-issue" }
                        if !issues.isEmpty {
                            Divider()
                            ScrollView(.horizontal) {
                                HStack(spacing: 14) {
                                    ForEach(issues) { item in
                                        Link(destination: item.url) {
                                            Label(item.label, systemImage: "circle.hexagongrid")
                                        }
                                        .accessibilityLabel("GitHub issue " + item.label)
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
                .navigationTitle(pane == .meridian ? "Meridian" : pane == .board ? "Sprint board" : (selected?.title ?? "Workstreams"))
                .toolbar {
                    Button { adding = true } label: { Label("New workstream", systemImage: "plus") }.keyboardShortcut("n")
                    Button { inspectorVisible.toggle() } label: {
                        Label(inspectorVisible ? "Hide inspector" : "Show inspector", systemImage: "sidebar.right")
                    }
                    .disabled(pane != .workstream)
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
        .onChange(of: pane == .workstream ? selectedTerminalID : nil, initial: true) { _, id in
            notifications.focusedTerminal = id
        }
        .onChange(of: store.rows.map(\.id)) { _, ids in
            selectNotification()
            if !ids.contains(selection ?? "") {
                selection = store.rows.first(where: Inbox.active.includes)?.id ?? ids.first
                if selected?.state == "Done" { expanded.insert(.done) }
            }
        }
        .sheet(isPresented: $adding) {
            NewStream(store: store, folder: recentFolders.first ?? "", recentFolders: recentFolders) { id, folder, terminal in
                if let terminal, let stream = store.rows.first(where: { $0.id == id }) {
                    rememberTerminal(terminal, in: stream)
                }
                lastFolder = folder
                if let data = try? JSONEncoder().encode(recentFolders) {
                    recentFoldersJSON = String(decoding: data, as: UTF8.self)
                }
                pane = .workstream
                workstreamQuery = ""
                selection = id
                agentSelection = nil
                archivedAgent = nil
                expanded.insert(store.rows.first { $0.id == id }?.state == "Done" ? .done : .active)
                adding = false
            }
        }
        .alert("Workstream error", isPresented: Binding(get: { !store.error.isEmpty && !adding }, set: { if !$0 { store.error = "" } })) {
            Button("OK") { store.error = "" }
        } message: { Text(store.error) }
    }

    private var closeTerminalAction: (() -> Void)? {
        guard pane == .workstream, !store.busy, !switching,
              let stream = selected, let terminal = selectedTerminal(stream) else { return nil }
        return { closeTerminal(terminal, in: stream) }
    }

    /// Opening a workstream from the board also reveals it in the sidebar.
    private func openFromOverview(_ stream: Stream) {
        workstreamQuery = ""
        selectTerminal(stream, agent: nil)
        expanded.insert(stream.state == "Done" ? .done : .active)
    }

    private func selectNotification() {
        guard let id = notifications.destination,
              let stream = store.rows.first(where: { $0.id == id }) else { return }
        workstreamQuery = ""
        if let terminalID = notifications.terminalDestination {
            rememberTerminal(terminalID, in: stream)
        }
        selectTerminal(stream, agent: nil)
        expanded.insert(stream.state == "Done" ? .done : .active)
        notifications.destination = nil
        notifications.terminalDestination = nil
    }

    private func inspector(_ stream: Stream) -> some View {
        WorkstreamInspector(stream: stream,
                            terminal: selectedTerminal(stream), busy: store.busy, blockerDestination: $blockerDestination,
                            clearPipeline: { Task { _ = await store.perform(["pipeline-reset", stream.id]) } },
                            pulls: store.pulls, viewingParent: agentSelection == nil && !switching, refreshPulls: { await store.refreshPulls($0) }) { keepTasksOpen in
            let reopening = stream.state == "Done"
            let args = [reopening ? "reopen" : "close", stream.id] + (keepTasksOpen && !reopening ? ["--force"] : [])
            Task {
                if await store.perform(args) != nil, reopening {
                    expanded.insert(.active)
                }
            }
        }
    }
}

private struct CloseWorkstreamTerminalKey: FocusedValueKey {
    typealias Value = () -> Void
}

private struct CloseCommandTitleKey: FocusedValueKey {
    typealias Value = String
}

extension FocusedValues {
    var closeWorkstreamTerminal: (() -> Void)? {
        get { self[CloseWorkstreamTerminalKey.self] }
        set { self[CloseWorkstreamTerminalKey.self] = newValue }
    }

    /// The close command's title in a window that closes something other than a terminal.
    var closeCommandTitle: String? {
        get { self[CloseCommandTitleKey.self] }
        set { self[CloseCommandTitleKey.self] = newValue }
    }
}

struct TerminalCommands: Commands {
    @FocusedValue(\.closeWorkstreamTerminal) private var closeTerminal
    @FocusedValue(\.closeCommandTitle) private var closeTitle

    var body: some Commands {
        CommandGroup(replacing: .appTermination) {
            Button("Quit Meridian") { NSApp.terminate(nil) }
                .keyboardShortcut("q", modifiers: .command)
        }
        CommandGroup(after: .newItem) {
            Button(closeTitle ?? "Close Terminal") { closeTerminal?() }
                .keyboardShortcut("w", modifiers: .command)
                .disabled(closeTerminal == nil)
        }
    }
}

struct NewStream: View {
    @ObservedObject var store: Store
    @Environment(\.dismiss) var dismiss
    @State var folder: String
    var recentFolders: [String]
    var created: (String, String, String?) -> Void
    @State private var reference = ""
    private var githubURL: String? {
        let value = reference.trimmingCharacters(in: .whitespacesAndNewlines)
        return value.range(of: #"(?i)^https?://github\.com/[\w.-]+/[\w.-]+/(pull|issues)/[1-9]\d*(?:[/?#].*)?$"#,
                           options: .regularExpression) == nil ? nil : value
    }
    @State private var preview: LinearPreview?
    @State private var previewed = ""
    @State private var lookupError = ""
    @State private var retry = 0

    private var validPreview: LinearPreview? { previewed == reference ? preview : nil }
    private var primaryAction: String {
        if githubURL != nil { return "Open workstream" }
        if let result = validPreview, result.existing != nil {
            return result.closed ? "Reopen workstream" : "Resume workstream"
        }
        return "Create"
    }
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
            if !recentFolders.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Recent folders").font(.caption).foregroundStyle(.secondary)
                    ScrollView {
                        VStack(spacing: 2) {
                            ForEach(recentFolders, id: \.self) { path in
                                Button {
                                    folder = path
                                } label: {
                                    HStack(spacing: 8) {
                                        Image(systemName: "folder").foregroundStyle(.secondary)
                                        VStack(alignment: .leading, spacing: 2) {
                                            Text(URL(fileURLWithPath: path).lastPathComponent)
                                                .font(.callout).lineLimit(1)
                                            Text((path as NSString).abbreviatingWithTildeInPath)
                                                .font(.caption).foregroundStyle(.secondary)
                                                .lineLimit(1).truncationMode(.middle)
                                        }
                                        Spacer(minLength: 0)
                                        if folder == path {
                                            Image(systemName: "checkmark").foregroundStyle(Color.accentColor)
                                        }
                                    }
                                    .padding(6).contentShape(Rectangle())
                                    .background(folder == path ? Color.accentColor.opacity(0.1) : Color.clear,
                                                in: RoundedRectangle(cornerRadius: 6))
                                }
                                .buttonStyle(.plain).help(path)
                                .accessibilityLabel("Use folder \(path)")
                            }
                        }
                    }
                    .frame(height: CGFloat(min(recentFolders.count, 4)) * 48)
                }
            }
            Text("Git repositories get a new branch and worktree shared by all terminals. Other folders are used directly.")
                .font(.caption).foregroundStyle(.secondary)
            TextField("GitHub issue, pull request, or Linear reference (optional)", text: $reference)
                .task(id: "\(reference)|\(retry)") {
                    let value = reference
                    preview = nil
                    lookupError = ""
                    guard githubURL == nil, !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
                    do {
                        try await Task.sleep(for: .milliseconds(350))
                        let data = try await Bridge.run(["linear-preview", value])
                        let result = try JSONDecoder().decode(LinearPreview.self, from: data)
                        guard !Task.isCancelled, reference == value else { return }
                        preview = result
                        previewed = value
                    } catch {
                        if !Task.isCancelled && reference == value { lookupError = error.localizedDescription }
                    }
                }
            if let issue = validPreview?.issue {
                Text("\(issue.identifier) · \(issue.title)").fontWeight(.semibold)
                Text("\(issue.status) · \(issue.assignee)").font(.callout)
                if let existing = store.rows.first(where: { $0.id == validPreview?.existing }) {
                    Text("Workstream · \(existing.title)").font(.callout).foregroundStyle(.secondary)
                }
            } else if !lookupError.isEmpty {
                Text(lookupError).font(.callout).foregroundStyle(.secondary)
                HStack {
                    Button("Retry lookup") { retry += 1 }
                    SettingsLink { Text("Settings…") }
                    Button("Create with reference only") { start(linkOnly: true) }
                        .disabled(store.busy || folder.isEmpty)
                }
            } else if githubURL == nil && !reference.isEmpty { ProgressView().controlSize(.small) }
            if !store.error.isEmpty { Text(store.error).foregroundStyle(.red) }
            HStack {
                Spacer()
                Button("Cancel") { store.error = ""; dismiss() }.keyboardShortcut(.cancelAction)
                if store.busy { ProgressView().controlSize(.small) }
                Button(primaryAction) { start() }.keyboardShortcut(.defaultAction)
                    .disabled(githubURL == nil && ((!reference.isEmpty && validPreview == nil) ||
                              (validPreview?.existing == nil && folder.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)))
            }.disabled(store.busy)
        }.padding(24).frame(width: 450)
    }

    private func start(linkOnly: Bool = false) {
        let args = githubURL.map { ["github-open", $0, folder, provider] } ?? (!linkOnly && validPreview != nil
            ? ["linear-start", reference, folder, provider, validPreview?.existing ?? "", validPreview?.issue.id ?? "",
               validPreview?.closed == true ? "1" : "0"]
            : ["create", "", folder, reference, provider])
        Task {
            guard let id = await store.perform(args) else { retry += 1; return }
            let row = store.rows.first { $0.id == id }
            if row?.state == "Done" {
                created(id, row?.sourceFolder ?? folder, nil)
                return
            }
            let agents = row?.terminals.filter { $0.provider != "shell" } ?? []
            let terminal = agents.first(where: \.live) ?? agents.first
            var terminalID = terminal?.id ?? id
            if row != nil && agents.isEmpty {
                terminalID = await store.perform(["add-terminal", id, provider]) ?? id
            } else if terminal?.live != true {
                _ = await store.perform(["open", terminalID])
            }
            created(id, row?.sourceFolder ?? folder, terminalID)
        }
    }
}

@main struct MeridianApp: App {
    init() {
        let defaults = UserDefaults.standard
        let domain = "local.meridian.app"
        if !defaults.bool(forKey: "didMigrateBrandPreferences"),
           let previous = defaults.persistentDomain(forName: "local.muxteroid.app") {
            var preferences = previous.merging(defaults.persistentDomain(forName: domain) ?? [:]) { _, current in current }
            preferences["didMigrateBrandPreferences"] = true
            defaults.setPersistentDomain(preferences, forName: domain)
        }
    }

    @NSApplicationDelegateAdaptor(AttentionNotifications.self) private var notifications
    @Environment(\.openWindow) private var openWindow
    var body: some Scene {
        Window("Meridian", id: "dashboard") {
            Dashboard(store: notifications.store, notifications: notifications, manager: notifications.manager)
        }.defaultSize(width: 1080, height: 760)
        .commands { TerminalCommands() }
        .onChange(of: notifications.destination) { _, id in
            if id != nil { openWindow(id: "dashboard") }
        }
        Settings { AppSettingsView() }
        WindowGroup("Document", id: "document", for: URL.self) { $url in
            if let url { DocumentWindow(url: url) }
        }.defaultSize(width: 760, height: 820)
        .commandsRemoved()
    }
}
