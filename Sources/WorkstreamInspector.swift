import SwiftUI
import MarkdownUI

struct WorkstreamInspector: View {
    let stream: Stream
    let terminal: WorkstreamTerminal
    let busy: Bool
    let toggleCompletion: (_ keepTasksOpen: Bool) -> Void

    @State private var confirmingCompletion = false
    private var unfinishedTasks: [BeadsTask] { stream.tasks.filter { $0.status != "closed" } }

    @AppStorage("workstreamDetailExpanded") private var detailExpanded = true

    private let statusOrder = ["blocked", "in_progress", "open", "deferred", "closed"]
    private var groups: [String] {
        let statuses = Set(stream.tasks.map(\.status))
        let other = statuses.subtracting(statusOrder).sorted()
        return statusOrder.dropLast().filter(statuses.contains) + other + (statuses.contains("closed") ? ["closed"] : [])
    }

    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 9) {
                HStack(alignment: .firstTextBaseline) {
                    Text("Tasks").font(.title3.bold())
                    Spacer()
                    if !stream.tasks.isEmpty {
                        let done = stream.tasks.filter { $0.status == "closed" }.count
                        Text("\(done) of \(stream.tasks.count) done")
                            .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                    }
                }
                if !stream.tasks.isEmpty {
                    ProgressView(value: Double(stream.tasks.filter { $0.status == "closed" }.count),
                                 total: Double(stream.tasks.count))
                        .progressViewStyle(.linear).tint(.accentColor).accessibilityLabel("Tasks complete")
                }
            }.padding(16)

            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    if stream.tasks.isEmpty { Text("No tasks yet").foregroundStyle(.secondary) }
                    ForEach(groups, id: \.self) { status in
                        taskGroup(status)
                    }
                    if !terminal.detail.isEmpty && terminal.detail != "Claude is continuing." && terminal.detail != "Claude is working on your request." {
                        Divider()
                        DisclosureGroup(isExpanded: $detailExpanded) {
                            Markdown(terminal.detail)
                                .markdownTextStyle { FontSize(13) }
                                .markdownImageProvider(.asset)
                                .markdownInlineImageProvider(.asset)
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.top, 6)
                        } label: {
                            Text(terminal.state == "Review" ? terminal.title + "’s reply" : "Activity").font(.headline)
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 16).padding(.bottom, 16)
            }.frame(maxHeight: .infinity)

            Divider()
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 6) {
                    Circle().fill(stateColor(stream.displayState)).frame(width: 7, height: 7)
                    Text(stream.displayState).font(.callout.weight(.semibold))
                }
                Label(URL(fileURLWithPath: stream.sourceFolder ?? stream.folder).lastPathComponent, systemImage: "folder")
                    .font(.callout).foregroundStyle(.secondary).help(stream.folder)
                    .accessibilityLabel(stream.folder)
                if let branch = stream.worktreeBranch {
                    Label(branch, systemImage: "arrow.triangle.branch")
                        .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                }
                if stream.terminals.count > 1 {
                    Text("\(stream.terminals.count) terminals · shared task list").font(.caption).foregroundStyle(.secondary)
                }
                if !stream.reference.isEmpty { Text(stream.reference).font(.callout).textSelection(.enabled) }
                Button(stream.state == "Done" ? "Reopen" : "Mark complete") {
                    if stream.state != "Done" && !unfinishedTasks.isEmpty {
                        confirmingCompletion = true
                    } else {
                        toggleCompletion(false)
                    }
                }
                .disabled(busy)
                .alert("Mark this workstream complete?", isPresented: $confirmingCompletion) {
                    Button("Cancel", role: .cancel) { }
                    Button("Mark complete") { toggleCompletion(true) }
                } message: {
                    let count = unfinishedTasks.count
                    let titles = unfinishedTasks.prefix(3).map(\.title).joined(separator: "\n")
                    let remaining = count > 3 ? "\n…and \(count - 3) more" : ""
                    Text("\(count) \(count == 1 ? "task will" : "tasks will") stay open in this workstream.\n\n\(titles)\(remaining)")
                }
            }.frame(maxWidth: .infinity, alignment: .leading).padding(16)
                .background(.bar)
        }
    }

    private func taskGroup(_ status: String) -> some View {
        let style: (name: String, icon: String, color: Color)
        switch status {
        case "blocked": style = ("Blocked", "exclamationmark.octagon.fill", .orange)
        case "in_progress": style = ("In progress", "circle.lefthalf.filled", .blue)
        case "open": style = ("Open", "circle", .secondary)
        case "closed": style = ("Completed", "checkmark.circle.fill", .green)
        default: style = (status.replacingOccurrences(of: "_", with: " ").capitalized, "clock", .secondary)
        }
        let tasks = stream.tasks.filter { $0.status == status }
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: style.icon).foregroundStyle(style.color)
                Text(style.name).fontWeight(.semibold)
            }.font(.callout)
            ForEach(tasks) { task in
                if task.id != tasks.first?.id { Divider() }
                Text(task.title)
                    .font(.body).lineSpacing(2)
                    .foregroundStyle(status == "closed" ? Color.secondary : Color.primary)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
                if let reason = task.blockerReason {
                    Text(reason)
                        .font(.callout).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
            }
        }
    }
}
