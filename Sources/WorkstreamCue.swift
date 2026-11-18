import SwiftUI

enum WorkstreamCue {
    case input(WorkstreamTerminal, Subagent?)
    case blocked(BeadsTask)
    case complete

    var title: String {
        switch self {
        case let .input(terminal, agent): return (agent?.title ?? terminal.title) + " needs input"
        case .blocked: return "View blocked tasks"
        case .complete: return "Ready to complete"
        }
    }

    var help: String {
        switch self {
        case let .input(terminal, agent):
            return (agent?.title ?? terminal.title) + " is waiting for your input. Open the conversation."
        case let .blocked(task):
            return "See what’s blocking “" + task.title + "” and how to unblock it."
        case .complete:
            return "All tasks are done. Open the inspector to mark this workstream complete."
        }
    }

    var symbol: String {
        switch self {
        case .input: return "text.bubble.fill"
        case .blocked: return "exclamationmark.octagon.fill"
        case .complete: return "checkmark.seal.fill"
        }
    }

    var color: Color {
        switch self {
        case .input, .blocked: return .orange
        case .complete: return .green
        }
    }
}

extension Stream {
    var nextCue: WorkstreamCue? {
        guard state != "Done" else { return nil }
        for terminal in terminals where terminal.live {
            if terminal.state == "Needs you" { return .input(terminal, nil) }
            if let agent = terminal.agents.first(where: { $0.state == "Needs you" }) {
                return .input(terminal, agent)
            }
        }
        if let task = tasks.first(where: { $0.status == "blocked" }) { return .blocked(task) }
        guard state != "Working", state != "Needs you",
              !agents.contains(where: { $0.state == "Working" }),
              !tasks.isEmpty, tasks.allSatisfy({ $0.status == "closed" }) else { return nil }
        return .complete
    }
}
