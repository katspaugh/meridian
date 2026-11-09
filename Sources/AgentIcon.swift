import SwiftUI

struct AgentIcon: View {
    let provider: String

    var body: some View {
        if provider == "shell" {
            Image(systemName: "terminal").frame(width: 16, height: 16).accessibilityHidden(true)
        } else {
        Image(provider == "codex" ? "Codex" : "Claude", bundle: .module)
            .resizable()
            .scaledToFit()
            .frame(width: 16, height: 16)
            .foregroundStyle(.primary)
            .accessibilityHidden(true)
        }
    }
}
