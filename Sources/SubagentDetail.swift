import SwiftUI
import MarkdownUI

struct Subagent: Decodable, Identifiable {
    let id, title, role, state, revision: String
}

private struct AgentMessage: Decodable {
    let label, text: String
}

struct SubagentDetail: View {
    let streamID: String
    let agent: Subagent
    let returnToTerminal: () -> Void
    @State private var messages: [AgentMessage] = []
    @State private var error = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 8) {
                Button(action: returnToTerminal) { Label("Workstream terminal", systemImage: "chevron.left") }
                    .buttonStyle(.link)
                Text(agent.title).font(.title2.bold())
                HStack {
                    Text(agent.role).foregroundStyle(.secondary)
                    Text(agent.state).foregroundStyle(stateColor(agent.state))
                }.font(.callout)
            }.padding()
            Divider()
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 14) {
                    if !error.isEmpty { Text(error).foregroundStyle(.red) }
                    else if messages.isEmpty { Text("No recorded messages yet").foregroundStyle(.secondary) }
                    ForEach(messages.indices, id: \.self) { index in
                        let message = messages[index]
                        if message.label == "Tool" {
                            Label(message.text, systemImage: "wrench").font(.caption.monospaced()).foregroundStyle(.secondary)
                        } else {
                            VStack(alignment: .leading, spacing: 5) {
                                Text(message.label).font(.headline)
                                Markdown(message.text)
                                    .markdownImageProvider(.asset).markdownInlineImageProvider(.asset)
                                    .textSelection(.enabled)
                            }
                        }
                    }
                }.padding().frame(maxWidth: .infinity, alignment: .leading)
            }
        }.frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .task(id: agent.revision + agent.state) {
            do {
                let data = try await Bridge.run(["agent-messages", streamID, agent.id])
                guard !Task.isCancelled else { return }
                messages = try JSONDecoder().decode([AgentMessage].self, from: data)
                error = ""
            } catch { if !Task.isCancelled { self.error = error.localizedDescription } }
        }
    }
}
