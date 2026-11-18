import SwiftUI
import AppKit

/// GitHub's view of a linked pull request. Every field is optional so a partial
/// reply still shows a row; without a reply the row is a plain link.
struct PullRequest: Decodable {
    let title, state, head: String?
    let failures: [FailedCheck]?
    let attention: [String]?
    let additions, deletions, checks, finished, failed: Int?
}

struct FailedCheck: Decodable {
    let name: String
    let url: URL?
}

private struct ReviewThread: Decodable, Identifiable {
    let url: URL
    let author: String?
    let body, path: String
    let line: Int?
    let outdated: Bool
    var id: URL { url }
    var location: String { path + (line.map { ":\($0)" } ?? "") + (outdated ? " (outdated)" : "") }
}

struct WorkstreamChanges: View {
    let links: [WorkstreamLink]
    let pulls: [String: PullRequest]
    let streamID: String
    let terminal: WorkstreamTerminal?

    @State private var showingAll = false
    private let limit = 5

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Changes").font(.headline)
            ForEach(showingAll ? links : Array(links.prefix(limit))) { link in
                PullRequestRow(link: link, pull: pulls[link.url.absoluteString], streamID: streamID, terminal: terminal)
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
    let streamID: String
    let terminal: WorkstreamTerminal?
    @State private var threads = [ReviewThread]()
    @State private var threadError = ""
    @State private var sending = false
    @State private var feedback = ""
    @State private var loadAttempt = 0

    private var canSend: Bool {
        guard let terminal else { return false }
        return terminal.terminalSocket != nil && terminal.provider != "shell"
            && ["Ready", "Review"].contains(terminal.state) && !sending
    }

    private func handoff(_ kind: String, _ details: [String]) {
        guard let terminal, canSend else { return }
        sending = true
        feedback = "Sending to " + terminal.title + "…"
        Task {
            defer { sending = false }
            do {
                _ = try await Bridge.run(["github-handoff", streamID, terminal.id, kind, link.url.absoluteString] + details)
                feedback = "Sent to " + terminal.title
            } catch { feedback = error.localizedDescription }
        }
    }

    private var isOpen: Bool { pull?.state == "OPEN" || pull?.state == "DRAFT" }

    @ViewBuilder private var followUp: some View {
        if let failures = pull?.failures, !failures.isEmpty {
            DisclosureGroup(failures.count == 1 ? failures[0].name + " failed" : "\(failures.count) failed checks") {
                ForEach(Array(failures.enumerated()), id: \.offset) { _, check in
                    VStack(alignment: .leading, spacing: 4) {
                        if failures.count > 1 { Text(check.name).fontWeight(.medium) }
                        Link("Open failed job", destination: check.url ?? link.url.appendingPathComponent("checks"))
                        Button("Investigate with agent") {
                            handoff("check", [check.name, check.url?.absoluteString ?? "", pull?.head ?? "unknown"])
                        }.disabled(!canSend)
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 3)
                }
            }
        }
        if !threads.isEmpty && isOpen {
            DisclosureGroup("\(threads.count) unresolved " + (threads.count == 1 ? "thread" : "threads")) {
                ForEach(threads) { thread in
                    VStack(alignment: .leading, spacing: 4) {
                        Link(thread.location, destination: thread.url)
                        Text(thread.body).lineLimit(4).textSelection(.enabled)
                        Button("Address with agent") {
                            handoff("thread", [thread.location, (thread.author ?? "Reviewer") + "\n" + thread.body,
                                               thread.url.absoluteString])
                        }.disabled(!canSend)
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 3)
                }
            }
        }
        if isOpen && !threadError.isEmpty {
            Button("Retry loading review threads") { loadAttempt += 1 }.help(threadError)
        }
        if !feedback.isEmpty { Text(feedback).foregroundStyle(.secondary) }
        else if (pull?.failures?.isEmpty == false || !threads.isEmpty) && !canSend {
            Text("Select a live, idle agent to send work.").foregroundStyle(.secondary)
        }
    }

    private func refreshThreads() async {
        guard isOpen else { threads = []; threadError = ""; return }
        while !Task.isCancelled {
            do {
                let data = try await Bridge.run(["review-threads", link.url.absoluteString])
                guard !Task.isCancelled else { return }
                threads = try JSONDecoder().decode([ReviewThread].self, from: data)
                threadError = ""
            } catch {
                guard !Task.isCancelled else { return }
                threads = []
                threadError = error.localizedDescription
            }
            do { try await Task.sleep(for: .seconds(60)) } catch { return }
        }
    }

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
        VStack(alignment: .leading, spacing: 5) {
            row
            followUp.font(.callout)
        }
        .task(id: "\(isOpen)-\(loadAttempt)") { await refreshThreads() }
    }

    private var row: some View {
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
