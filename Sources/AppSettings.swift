import SwiftUI

private struct AppPreferences: Decodable {
    let paused, watchComments, linearStored, linearEnvironment: Bool
}
private struct SettingsResult: Decodable { let ok: Bool; let message: String }

struct AppSettingsView: View {
    @State private var paused = true
    @State private var watchComments = true
    @State private var linearStored = false
    @State private var linearEnvironment = false
    @State private var key = ""
    @State private var busy = false
    @State private var loaded = false
    @State private var message = ""
    @State private var failed = false
    @State private var removing = false

    private func load(updatePreferences: Bool = true) async throws {
        let data = try await Bridge.run(["settings", "state"])
        let state = try JSONDecoder().decode(AppPreferences.self, from: data)
        if updatePreferences {
            paused = state.paused
            watchComments = state.watchComments
        }
        linearStored = state.linearStored
        linearEnvironment = state.linearEnvironment
        loaded = true
    }

    private func perform(_ action: String, payload: [String: Any]? = nil) {
        Task {
            busy = true
            failed = false
            defer { busy = false }
            do {
                let input = try payload.map { try JSONSerialization.data(withJSONObject: $0) }
                let data = try await Bridge.run(["settings", action], input: input)
                let result = try JSONDecoder().decode(SettingsResult.self, from: data)
                message = result.message
                if action == "save-linear" { key = "" }
                try await load(updatePreferences: action == "preferences")
            } catch {
                failed = true
                message = error.localizedDescription
            }
        }
    }

    var body: some View {
        Form {
            Section("Meridian") {
                Toggle("Pause the soul", isOn: $paused)
                Toggle("Watch Linear and GitHub comments", isOn: $watchComments)
                Text("While Meridian is open, check about every five minutes. Pausing the soul pauses comment watching and agent follow-ups.")
                    .font(.caption).foregroundStyle(.secondary)
                Button("Save preferences") {
                    perform("preferences", payload: ["paused": paused, "watchComments": watchComments])
                }
            }
            Section("Linear") {
                Text(linearStored ? "API key saved in macOS Keychain" : "No API key saved")
                    .foregroundStyle(.secondary)
                if linearEnvironment {
                    Text("A key from the app’s launch environment takes precedence over the saved key.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                SecureField(linearStored ? "Replacement API key" : "Personal API key", text: $key)
                    .textContentType(.password)
                HStack {
                    Button("Verify and save key") { perform("save-linear", payload: ["key": key]) }
                        .disabled(key.isEmpty)
                    Button("Test connection") { perform("test-linear") }
                        .disabled(!linearStored && !linearEnvironment)
                    if linearStored {
                        Button("Remove key", role: .destructive) { removing = true }
                    }
                }
                Link("Create a Linear API key", destination: URL(string: "https://linear.app/settings/api")!)
                    .font(.caption)
                Text("Use read access for issue context, comment creation to ask questions, and write access to share review attachments.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("GitHub") {
                Text("Uses your GitHub CLI account. Run gh auth login in Terminal to connect it.")
                    .font(.callout).foregroundStyle(.secondary)
                Button("Test GitHub connection") { perform("test-github") }
            }
            if !message.isEmpty {
                Text(message).font(.callout).foregroundStyle(failed ? Color.red : Color.secondary)
                    .textSelection(.enabled)
            }
            if busy { ProgressView().controlSize(.small) }
        }
        .formStyle(.grouped)
        .frame(width: 560, height: 620)
        .disabled(busy || !loaded)
        .task {
            busy = true
            defer { busy = false }
            do { try await load() }
            catch { failed = true; message = error.localizedDescription }
        }
        .onDisappear { key = "" }
        .confirmationDialog("Remove the saved Linear key?", isPresented: $removing) {
            Button("Remove key", role: .destructive) { perform("remove-linear") }
        } message: {
            Text("Linear monitoring will stop unless a key is supplied by the app’s launch environment.")
        }
    }
}
