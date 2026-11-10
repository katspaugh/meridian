import SwiftUI
import AppKit

/// GitHub's view of a linked pull request. Every field is optional so a partial
/// reply still shows a row; without a reply the row is a plain link.
struct PullRequest: Decodable {
    let title, state: String?
    let additions, deletions, checks, finished, failed: Int?
}

struct WorkstreamChanges: View {
    let links: [WorkstreamLink]
    let pulls: [String: PullRequest]

    @State private var showingAll = false
    private let limit = 5

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Changes").font(.headline)
            ForEach(showingAll ? links : Array(links.prefix(limit))) { link in
                PullRequestRow(link: link, pull: pulls[link.url.absoluteString])
            }
            if links.count > limit {
                Button(showingAll ? "Show less" : "Show more") { showingAll.toggle() }
                    .buttonStyle(.plain).foregroundStyle(.secondary)
            }
        }
    }
}

private struct PullRequestRow: View {
    let link: WorkstreamLink
    let pull: PullRequest?

    private var title: String? {
        let title = pull?.title?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return title.isEmpty ? nil : title
    }

    /// "owner/repo#12" reads as "repo #12" beside the title.
    private var reference: String {
        let parts = link.label.split(separator: "#", maxSplits: 1)
        guard parts.count == 2, let repository = parts[0].split(separator: "/").last else { return link.label }
        return "\(repository) #\(parts[1])"
    }

    private var status: (name: String, icon: String, color: Color) {
        switch pull?.state {
        case "OPEN": return ("Open", "arrow.triangle.pull", .green)
        case "DRAFT": return ("Draft", "arrow.triangle.pull", .secondary)
        case "MERGED": return ("Merged", "arrow.triangle.merge", .purple)
        case "CLOSED": return ("Closed", "arrow.triangle.pull", .red)
        default: return ("", "arrow.triangle.pull", .secondary)
        }
    }

    private var checks: (summary: String, color: Color, progress: String?)? {
        guard let total = pull?.checks, total > 0 else { return nil }
        let finished = min(pull?.finished ?? 0, total), failed = pull?.failed ?? 0
        let progress = finished < total ? "\(finished)/\(total)" : nil
        if failed > 0 { return ("\(failed) of \(total) checks failed", .red, progress) }
        if finished < total { return ("\(finished) of \(total) checks finished", .secondary, progress) }
        return (total == 1 ? "Check passed" : "All \(total) checks passed", .green, nil)
    }

    private var summary: String {
        var parts = ["Pull request " + link.label]
        if let title { parts.append(title) }
        if !status.name.isEmpty { parts.append(status.name) }
        if let additions = pull?.additions, let deletions = pull?.deletions {
            parts.append("\(additions) added, \(deletions) removed")
        }
        if let checks { parts.append(checks.summary) }
        return parts.joined(separator: " · ")
    }

    private func open() { NSWorkspace.shared.open(link.url) }

    @ViewBuilder private var actions: some View {
        Button("Open in Browser", action: open)
        Button("Copy Link") {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(link.url.absoluteString, forType: .string)
        }
    }

    var body: some View {
        Button(action: open) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: status.icon).foregroundStyle(status.color).frame(width: 16)
                VStack(alignment: .leading, spacing: 3) {
                    // The trailing space belongs to the actions menu laid over the row.
                    Text(title ?? link.label).lineLimit(1).padding(.trailing, 26)
                    if title != nil {
                        HStack(spacing: 5) {
                            Text(reference).foregroundStyle(.secondary).lineLimit(1)
                            Group {
                                if let additions = pull?.additions { Text(verbatim: "+\(additions)").foregroundStyle(.green) }
                                if let deletions = pull?.deletions { Text(verbatim: "−\(deletions)").foregroundStyle(.red) }
                            }.fixedSize()
                            Spacer(minLength: 4)
                            if let checks {
                                HStack(spacing: 4) {
                                    Circle().fill(checks.color).frame(width: 7, height: 7)
                                    if let progress = checks.progress { Text(verbatim: progress).foregroundStyle(.secondary) }
                                }.fixedSize()
                            }
                        }.font(.callout.monospacedDigit())
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(summary)
        .help(summary)
        .contextMenu { actions }
        .overlay(alignment: .topTrailing) {
            Menu { actions } label: {
                Image(systemName: "ellipsis").frame(width: 22, height: 16).contentShape(Rectangle())
            }
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
            .accessibilityLabel("Actions for " + reference)
        }
    }
}
