import SwiftUI

struct AgentIcon: View {
    let provider: String

    var body: some View {
        Image(provider == "codex" ? "Codex" : "Claude", bundle: .module)
            .resizable()
            .scaledToFit()
            .frame(width: 16, height: 16)
            .foregroundStyle(.primary)
            .accessibilityHidden(true)
    }
}
