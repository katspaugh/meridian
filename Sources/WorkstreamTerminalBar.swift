import SwiftUI

struct WorkstreamTerminalBar: View {
    let terminals: [WorkstreamTerminal]
    let selectedID: String
    let switching, canAdd: Bool
    let select: (WorkstreamTerminal) -> Void
    let add: (String) -> Void

    var body: some View {
        HStack(spacing: 4) {
            ScrollView(.horizontal) {
                HStack(spacing: 4) {
                    ForEach(terminals) { terminal in
                        tab(terminal)
                    }
                }
            }.scrollIndicators(.hidden)
            Menu {
                Button("Plain terminal", systemImage: "terminal") { add("shell") }
                Button { add("claude") } label: {
                    Label { Text("Claude") } icon: { AgentIcon(provider: "claude") }
                }
                Button { add("codex") } label: {
                    Label { Text("Codex") } icon: { AgentIcon(provider: "codex") }
                }
            } label: { Image(systemName: "plus") }
                .menuStyle(.borderlessButton).fixedSize().padding(.horizontal, 8)
                .help("Add terminal in this workstream").accessibilityLabel("Add terminal")
                .disabled(!canAdd)
        }.padding(5).background(.bar)
    }

    private func tab(_ terminal: WorkstreamTerminal) -> some View {
        Button { select(terminal) } label: {
            HStack(spacing: 6) {
                AgentIcon(provider: terminal.provider)
                Text(terminal.title)
                if terminal.state == "Needs you" {
                    Image(systemName: "exclamationmark.circle.fill").foregroundStyle(.orange)
                } else if ["Working", "Review"].contains(terminal.state) {
                    Circle().fill(stateColor(terminal.state)).frame(width: 6, height: 6)
                }
            }
            .padding(.horizontal, 9).padding(.vertical, 6)
            .background(selectedID == terminal.id ? Color.primary.opacity(0.09) : Color.clear,
                        in: RoundedRectangle(cornerRadius: 5))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain).disabled(switching)
        .accessibilityLabel(terminal.title + " · " + terminal.state)
        .accessibilityAddTraits(selectedID == terminal.id ? .isSelected : [])
        .help(terminal.title + " · " + terminal.state)
    }
}
