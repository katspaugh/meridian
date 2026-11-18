import SwiftUI

struct PipelineTask: Decodable, Identifiable {
    let n: Int
    let title, mark, implement, review: String
    let round: Int?
    let time: Double?
    let estimate: Bool
    var id: Int { n }
}

struct PipelinePhase: Decodable, Identifiable {
    let id, label, mark, detail: String
    let time: Double?
    let estimate: Bool
    let url: URL?
    let tasks: [PipelineTask]?
}

struct Pipeline: Decodable {
    let name: String
    let startedAt: Double
    let finishAt: Double?
    let finished: String?
    let needsYou, polling, frozen: Bool
    let activeAgents: Int
    let phases: [PipelinePhase]
}

struct WorkstreamDocument: Decodable, Identifiable {
    let kind, title, path: String
    var id: String { path }
}

/// The bridge sends epoch seconds; these turn them into the words and clock times the section shows.
enum PipelineText {
    static func clock(_ time: Double, estimate: Bool) -> String {
        let text = Date(timeIntervalSince1970: time).formatted(date: .omitted, time: .shortened)
        return estimate ? "≈" + text : text
    }

    static func duration(_ seconds: Double) -> String {
        let minutes = max(0, Int((seconds / 60).rounded()))
        return minutes < 60 ? "\(minutes)m" : "\(minutes / 60)h \(minutes % 60)m"
    }

    static func summary(_ pipeline: Pipeline, now: Date) -> String? {
        if pipeline.finished == "merged" {
            return pipeline.finishAt.map { "merged " + clock($0, estimate: false) } ?? "merged"
        }
        if pipeline.finished == "closed" { return "PR closed without merging" }
        if pipeline.frozen { return nil }
        if let finishAt = pipeline.finishAt {
            return "about \(duration(finishAt - now.timeIntervalSince1970)) to go → merged ≈ \(clock(finishAt, estimate: false))"
        }
        return "No estimate yet"
    }

    static func spoken(mark: String) -> String {
        switch mark {
        case "done": return "done"
        case "running": return "in progress"
        case "slow": return "running late"
        case "failed": return "failed"
        case "needs": return "needs you"
        default: return "to come"
        }
    }

    /// A time as VoiceOver should read it: "13:20", or "about 14:10" for an estimate.
    static func spokenClock(_ time: Double?, estimate: Bool) -> String? {
        guard let time else { return nil }
        let text = clock(time, estimate: false)
        return estimate ? "about " + text : text
    }
}

struct PipelineSection: View {
    let pipeline: Pipeline?
    let documents: [WorkstreamDocument]
    let clear: () -> Void
    var open: (WorkstreamDocument) -> Void = { _ in }

    @AppStorage("pipelineExpanded") private var expanded = true

    var body: some View {
        TimelineView(.everyMinute) { context in
            DisclosureGroup(isExpanded: $expanded) {
                content(now: context.date).padding(.top, 8)
            } label: {
                header(now: context.date)
            }
        }
    }

    private func header(now: Date) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text("Pipeline").font(.title3.bold())
            Spacer()
            if let pipeline, pipeline.finished == nil, !pipeline.frozen {
                Text("\(PipelineText.duration(now.timeIntervalSince1970 - pipeline.startedAt)) in")
                    .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            }
        }
        .contentShape(Rectangle())
        .contextMenu {
            Button("Clear Pipeline", action: clear).disabled(pipeline == nil)
        }
    }

    private func content(now: Date) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            if let pipeline {
                VStack(alignment: .leading, spacing: 3) {
                    Text(pipeline.name).font(.headline).lineLimit(1).truncationMode(.tail)
                    if let summary = PipelineText.summary(pipeline, now: now) {
                        Text(summary).font(.callout).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if pipeline.activeAgents > 0 {
                        Text("\(pipeline.activeAgents) \(pipeline.activeAgents == 1 ? "subagent" : "subagents") working")
                            .font(.callout).foregroundStyle(.secondary)
                    }
                }
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(pipeline.phases) { phase in
                        PipelinePhaseRow(phase: phase)
                        ForEach(phase.tasks ?? []) { task in
                            PipelineTaskRow(task: task)
                        }
                    }
                }
            }
            if !documents.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(documents) { document in
                        documentRow(document)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func documentRow(_ document: WorkstreamDocument) -> some View {
        let kind = document.kind == "plan" ? "Plan" : "Spec"
        return Button { open(document) } label: {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(kind).font(.caption).foregroundStyle(.secondary)
                Text(document.title).font(.callout).lineLimit(1).truncationMode(.tail)
                Spacer(minLength: 4)
                Image(systemName: "arrow.up.right").font(.caption).foregroundStyle(.secondary)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(document.path)
        .accessibilityLabel("Open \(kind.lowercased()): \(document.title)")
    }
}

/// How a mark looks: the symbol and colour for each status the bridge sends.
private func markStyle(_ mark: String) -> (symbol: String, color: Color) {
    switch mark {
    case "done": return ("checkmark.circle.fill", .green)
    case "running": return ("circle.lefthalf.filled", .blue)
    case "slow": return ("circle.lefthalf.filled", .red)
    case "failed": return ("xmark.circle.fill", .red)
    case "needs": return ("exclamationmark.circle.fill", .orange)
    default: return ("circle", .secondary)
    }
}

private struct PipelineMark: View {
    let mark: String
    var body: some View {
        let style = markStyle(mark)
        Image(systemName: style.symbol).foregroundStyle(style.color)
    }
}

/// A small square for a task's implement or review step.
private struct PipelineStepSquare: View {
    let mark: String
    var body: some View {
        if mark == "todo" {
            Image(systemName: "square").foregroundStyle(.secondary)
        } else {
            Image(systemName: "square.fill").foregroundStyle(markStyle(mark).color)
        }
    }
}

/// The leading time column. It is as wide as the widest estimate in the person's clock format,
/// so rows line up and nothing wraps into it.
private struct PipelineTimeColumn: View {
    let time: Double?
    let estimate: Bool

    private static var widest: String {
        let noon = Calendar.current.date(bySettingHour: 12, minute: 58, second: 0, of: Date()) ?? Date()
        return PipelineText.clock(noon.timeIntervalSince1970, estimate: true)
    }

    var body: some View {
        ZStack(alignment: .leading) {
            Text(Self.widest).hidden()
            if let time { Text(PipelineText.clock(time, estimate: estimate)) }
        }
        .font(.caption.monospacedDigit())
        .foregroundStyle(.secondary)
        .lineLimit(1)
        .fixedSize()
    }
}

private struct PipelinePhaseRow: View {
    let phase: PipelinePhase
    @Environment(\.openURL) private var openURL

    var body: some View {
        let emphasised = ["running", "slow", "needs"].contains(phase.mark)
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            PipelineTimeColumn(time: phase.time, estimate: phase.estimate)
            PipelineMark(mark: phase.mark)
            Text(phase.label)
                .fontWeight(emphasised ? .semibold : .regular)
                .foregroundStyle(phase.mark == "todo" ? Color.secondary : Color.primary)
                .lineLimit(1)
                .layoutPriority(1)
            if !phase.detail.isEmpty {
                Group {
                    if let url = phase.url {
                        Link(phase.detail, destination: url).help(url.absoluteString)
                    } else {
                        Text(phase.detail).foregroundStyle(.secondary)
                    }
                }
                .lineLimit(1).truncationMode(.tail)
                .padding(.leading, 4)
            }
            Spacer(minLength: 0)
        }
        .font(.callout)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityText)
        .modifier(PipelineLinkAction(url: phase.detail.isEmpty ? nil : phase.url, openURL: openURL))
    }

    private var accessibilityText: String {
        [phase.label, PipelineText.spoken(mark: phase.mark),
         PipelineText.spokenClock(phase.time, estimate: phase.estimate),
         phase.detail.isEmpty ? nil : phase.detail]
            .compactMap { $0 }.joined(separator: ", ")
    }
}

/// Keeps a phase's link usable once the row is read as one accessibility element.
private struct PipelineLinkAction: ViewModifier {
    let url: URL?
    let openURL: OpenURLAction

    func body(content: Content) -> some View {
        if let url {
            content.accessibilityAddTraits(.isLink).accessibilityAction { openURL(url) }
        } else {
            content
        }
    }
}

private struct PipelineTaskRow: View {
    let task: PipelineTask

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            PipelineTimeColumn(time: task.time, estimate: task.estimate)
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                PipelineMark(mark: task.mark)
                Text("\(task.n)").monospacedDigit().foregroundStyle(.secondary).fixedSize()
                Text(task.title)
                    .foregroundStyle(task.mark == "todo" ? Color.secondary : Color.primary)
                    .lineLimit(1).truncationMode(.tail)
                Spacer(minLength: 4)
                HStack(spacing: 2) {
                    PipelineStepSquare(mark: task.implement)
                    PipelineStepSquare(mark: task.review)
                }
                .font(.caption2).fixedSize()
                if let round = task.round {
                    Text("r\(round)").font(.caption.monospacedDigit()).foregroundStyle(.secondary).fixedSize()
                }
            }
            .padding(.leading, 16)
        }
        .font(.callout)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityText)
    }

    private var accessibilityText: String {
        ["Task \(task.n)", task.title, PipelineText.spoken(mark: task.mark),
         "implement " + PipelineText.spoken(mark: task.implement),
         "review " + PipelineText.spoken(mark: task.review),
         task.round.map { "fix round \($0)" },
         PipelineText.spokenClock(task.time, estimate: task.estimate)]
            .compactMap { $0 }.joined(separator: ", ")
    }
}
