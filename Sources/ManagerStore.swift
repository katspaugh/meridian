import SwiftUI

struct ManagerAsk: Decodable, Identifiable { let id: Int; let time: Double; let stream, terminal, summary, recommendation: String; let options: [String] }
struct ManagerState: Decodable { let sweepDue: Bool; let asks: [ManagerAsk] }

@MainActor final class ManagerStore: ObservableObject {
    @Published var state: ManagerState?
    @Published var sweeping = false
    private var refreshID = 0

    /// Returns whether a fresh state was loaded; on failure the previous state stays.
    @discardableResult func refresh() async -> Bool {
        refreshID += 1
        let id = refreshID
        guard let data = try? await Bridge.run(["manager-state"]),
              let loaded = try? JSONDecoder().decode(ManagerState.self, from: data),
              id == refreshID else { return false }
        state = loaded
        return true
    }

    func sweep(excluding terminal: String?) async {
        guard !sweeping else { return }
        sweeping = true
        defer { sweeping = false }
        _ = try? await Bridge.run(["manager-sweep"] + (terminal.map { [$0] } ?? []))
        await refresh()
    }

}
