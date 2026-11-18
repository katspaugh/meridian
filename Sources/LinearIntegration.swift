import SwiftUI

struct LinearIssue: Decodable {
    let id, identifier, title, status, assignee, description: String
    let url: URL
    let revision: String?
    let changes: [String]?
}

struct LinearPreview: Decodable {
    let issue: LinearIssue
    let existing: String?
    let closed: Bool
}

struct LinearQuestion: Decodable, Identifiable {
    var id: Int { ask }
    let ask: Int
    let url: URL
    let summary: String
    let reply: String?
    let pending: Bool
}

struct LinearQuestionAction: View {
    let stream: Stream
    let ask: ManagerAsk
    @State private var posting = false
    @State private var error = ""
    @State private var posted = false

    var body: some View {
        if stream.primary != nil {
            if let thread = stream.linearQuestions?.first(where: { $0.ask == ask.id }) {
                Link("View question in Linear", destination: thread.url)
            } else {
                Button(posted ? "Posted to Linear" : "Ask on Linear") {
                    posting = true
                    Task {
                        defer { posting = false }
                        do {
                            _ = try await Bridge.run(["linear-ask", stream.id, String(ask.id)])
                            error = ""
                            posted = true
                        } catch { self.error = error.localizedDescription }
                    }
                }.disabled(posting || posted)
                if !error.isEmpty { Text(error).font(.caption).foregroundStyle(.red) }
            }
        }
    }
}

struct LinearHandoff: View {
    let stream: Stream
    let prs: [WorkstreamLink]
    let pulls: [String: PullRequest]
    @Environment(\.dismiss) private var dismiss
    @State private var selection = ""
    @State private var verified = ""
    @State private var remaining = ""
    @State private var publishing = false
    @State private var error = ""

    private func checks(_ url: String) -> String {
        guard let pull = pulls[url], let count = pull.checks, count > 0 else { return "" }
        return "GitHub reports \(pull.finished ?? 0) of \(count) checks finished, \(pull.failed ?? 0) failed."
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Share for review").font(.title2.bold())
            if let issue = stream.primary { Text("\(issue.identifier) · \(issue.title)") }
            Picker("Pull request", selection: $selection) {
                ForEach(prs) { pr in Text(pr.label).tag(pr.url.absoluteString) }
            }
            TextField("What was verified?", text: $verified, axis: .vertical).lineLimit(2...6)
            TextField("What remains open?", text: $remaining, axis: .vertical).lineLimit(2...6)
            if !error.isEmpty { Text(error).foregroundStyle(.red) }
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Share to Linear") {
                    publishing = true
                    Task {
                        defer { publishing = false }
                        do {
                            _ = try await Bridge.run(["linear-share", stream.id, selection, verified, remaining])
                            dismiss()
                        } catch { self.error = error.localizedDescription }
                    }
                }.keyboardShortcut(.defaultAction)
                    .disabled(selection.isEmpty || verified.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                              || remaining.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }.disabled(publishing)
        }.padding(20).frame(width: 460)
        .onAppear {
            selection = prs.first?.url.absoluteString ?? ""
            verified = checks(selection)
            let openTasks = stream.tasks.filter { $0.status != "closed" }
            remaining = openTasks.isEmpty ? "No open Beads tasks." : openTasks.map(\.title).joined(separator: "\n")
        }
        .onChange(of: selection) { old, new in
            if verified == checks(old) { verified = checks(new) }
        }
    }
}
