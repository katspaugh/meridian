import SwiftUI

struct SprintBoard: View {
    let streams: [Stream]
    let openStream: (Stream) -> Void
    let newStream: () -> Void
    @State private var query = ""
    @State private var includeCompleted = true

    private let columnWidth: CGFloat = 230
    private let standardStatuses = ["open", "in_progress", "blocked", "deferred", "closed"]
    private var statuses: [String] {
        let extra = Set(streams.flatMap { $0.tasks.map(\.status) }).subtracting(standardStatuses).sorted()
        return Array(standardStatuses.dropLast()) + extra + ["closed"]
    }
    private var search: String { query.trimmingCharacters(in: .whitespacesAndNewlines) }
    private func matches(_ value: String) -> Bool { value.localizedCaseInsensitiveContains(search) }
    private func matchesStream(_ stream: Stream) -> Bool {
        search.isEmpty || matches(stream.title) || matches(stream.id) || matches(stream.folder) || matches(stream.reference)
    }
    private func tasks(_ stream: Stream) -> [BeadsTask] {
        matchesStream(stream) ? stream.tasks : stream.tasks.filter { matches($0.title) || matches($0.id) }
    }
    private var visibleStreams: [Stream] {
        streams.filter { (includeCompleted || $0.state != "Done") && (matchesStream($0) || !tasks($0).isEmpty) }
    }
    private func name(_ status: String) -> String {
        switch status {
        case "open": return "To do"
        case "in_progress": return "In progress"
        case "closed": return "Completed"
        default: return status.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }
    private func color(_ status: String) -> Color {
        switch status {
        case "in_progress": return .blue
        case "blocked": return .orange
        case "closed": return .green
        default: return .secondary
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 16) {
                TextField("Search workstreams or tasks", text: $query)
                    .textFieldStyle(.roundedBorder).frame(maxWidth: 320)
                    .accessibilityLabel("Search workstreams or tasks")
                Toggle("Include completed workstreams", isOn: $includeCompleted)
                Spacer()
                Text("\(visibleStreams.count) streams · \(visibleStreams.reduce(0) { $0 + tasks($1).count }) tasks")
                    .font(.callout).foregroundStyle(.secondary)
            }.padding(16)
            Divider()
            if streams.isEmpty {
                ContentUnavailableView {
                    Label("No workstreams yet", systemImage: "rectangle.split.3x1")
                } description: {
                    Text("Create a workstream to see its Beads tasks here.")
                } actions: {
                    Button("New workstream…", action: newStream)
                }
            } else if visibleStreams.isEmpty {
                ContentUnavailableView.search(text: search)
            } else {
                ScrollView([.horizontal, .vertical]) {
                    LazyVStack(alignment: .leading, spacing: 0, pinnedViews: [.sectionHeaders]) {
                        Section {
                            ForEach(visibleStreams) { stream in
                                lane(stream)
                                Divider()
                            }
                        } header: {
                            HStack(alignment: .center, spacing: 12) {
                                Text("Workstream").frame(width: columnWidth, alignment: .leading)
                                ForEach(statuses, id: \.self) { status in
                                    HStack {
                                        Circle().fill(color(status)).frame(width: 7, height: 7)
                                        Text(name(status))
                                        Spacer()
                                        Text("\(visibleStreams.reduce(0) { total, stream in total + tasks(stream).filter { $0.status == status }.count })")
                                            .foregroundStyle(.secondary).monospacedDigit()
                                    }.frame(width: columnWidth)
                                }
                            }.font(.callout.weight(.semibold)).padding(16).background(.bar)
                        }
                    }
                }
                .defaultScrollAnchor(.topLeading)
            }
        }.frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
    }

    private func lane(_ stream: Stream) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Button { openStream(stream) } label: {
                VStack(alignment: .leading, spacing: 9) {
                    Text(stream.title).font(.headline).fixedSize(horizontal: false, vertical: true)
                    Label(stream.displayState, systemImage: "circle.fill")
                        .font(.caption).foregroundStyle(stateColor(stream.displayState))
                    Text(URL(fileURLWithPath: stream.folder).lastPathComponent)
                        .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    Text(stream.tasks.isEmpty ? "No tasks yet" : "\(stream.tasks.filter { $0.status == "closed" }.count) of \(stream.tasks.count) done")
                        .font(.caption).foregroundStyle(.secondary)
                    Label("Open workstream", systemImage: "arrow.up.right")
                        .font(.caption).foregroundStyle(Color.accentColor)
                }.frame(width: columnWidth, alignment: .leading).contentShape(Rectangle())
            }.buttonStyle(.plain).help("Open \(stream.title)")
            ForEach(statuses, id: \.self) { status in
                VStack(alignment: .leading, spacing: 8) {
                    let cards = tasks(stream).filter { $0.status == status }
                    if cards.isEmpty {
                        Text("—").foregroundStyle(.tertiary).padding(12)
                    }
                    ForEach(cards) { task in
                        Button { openStream(stream) } label: {
                            VStack(alignment: .leading, spacing: 8) {
                                Text(task.title).font(.callout).fixedSize(horizontal: false, vertical: true)
                                TaskBlockerDetails(task: task).font(.caption)
                                Text(task.id).font(.caption.monospaced()).foregroundStyle(.secondary)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading).padding(12)
                            .background(color(status).opacity(0.07), in: RoundedRectangle(cornerRadius: 8))
                            .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(color(status).opacity(0.22)))
                            .contentShape(Rectangle())
                        }.buttonStyle(.plain)
                            .accessibilityLabel("\(task.title), \(name(status)), open \(stream.title)")
                            .accessibilityHint([task.blockerReason, task.blockerAction].compactMap { $0 }.joined(separator: ". "))
                    }
                }.frame(width: columnWidth, alignment: .leading)
            }
        }.padding(16)
    }
}
