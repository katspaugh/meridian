import SwiftUI
import MarkdownUI

struct WorkstreamInspector: View {
    let stream: Stream
    let busy: Bool
    let toggleCompletion: () -> Void

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
                    if !stream.detail.isEmpty && stream.detail != "Claude is continuing." && stream.detail != "Claude is working on your request." {
                        Divider()
                        DisclosureGroup(isExpanded: $detailExpanded) {
                            Markdown(stream.detail)
                                .markdownTextStyle { FontSize(13) }
                                .markdownImageProvider(.asset)
                                .markdownInlineImageProvider(.asset)
                                .textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.top, 6)
                        } label: {
                            Text(stream.state == "Review" ? "Claude’s reply" : "Activity").font(.headline)
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 16).padding(.bottom, 16)
            }.frame(maxHeight: .infinity)

            Divider()
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 6) {
                    Circle().fill(stateColor(stream.state)).frame(width: 7, height: 7)
                    Text(stream.state).font(.callout.weight(.semibold))
                }
                Label(URL(fileURLWithPath: stream.folder).lastPathComponent, systemImage: "folder")
                    .font(.callout).foregroundStyle(.secondary).help(stream.folder)
                    .accessibilityLabel(stream.folder)
                if !stream.reference.isEmpty { Text(stream.reference).font(.callout).textSelection(.enabled) }
                Button(stream.state == "Done" ? "Reopen" : "Mark complete", action: toggleCompletion)
                    .disabled(busy)
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
            }
        }
    }
}
