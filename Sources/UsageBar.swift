import SwiftUI

private struct UsageWindow: Decodable {
    let label: String
    let percent: Double
    let reset: TimeInterval?

    func countdown(at now: Date) -> String? {
        guard let reset else { return nil }
        let minutes = Int(ceil((reset - now.timeIntervalSince1970) / 60))
        if minutes <= 0 { return nil }
        if minutes >= 1440 { return "\(minutes / 1440)d \((minutes % 1440) / 60)h" }
        if minutes >= 60 { return "\(minutes / 60)h \(minutes % 60)m" }
        return "\(minutes)m"
    }
}

private struct ProviderUsage: Decodable {
    let windows: [UsageWindow]
    let measured: TimeInterval?
    let hint: String?
}

struct UsageBar: View {
    @State private var readings: [String: ProviderUsage] = [:]
    @State private var loading = false
    @State private var failed = false

    var body: some View {
        TimelineView(.periodic(from: .now, by: 60)) { timeline in
            HStack(spacing: 14) {
                Link(destination: URL(string: "https://claude.ai/settings/usage")!) {
                    group("Claude", id: "claude", icon: "sun.max.fill", tint: .orange, now: timeline.date)
                }
                .accessibilityHint("Opens Claude account usage in your browser")
                Divider().frame(height: 12)
                group("Codex", id: "codex", icon: "terminal", tint: .primary, now: timeline.date)
                Button {
                    Task { await refresh() }
                } label: {
                    Image(systemName: "arrow.clockwise").frame(width: 20, height: 20)
                }
                .buttonStyle(.plain)
                .disabled(loading)
                .accessibilityLabel("Refresh account usage")
                .help("Refresh account usage")
                Spacer(minLength: 0)
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            .padding(.horizontal, 12)
            .frame(height: 28)
            .background(.bar)
        }
        .task {
            while !Task.isCancelled {
                await refresh()
                do { try await Task.sleep(for: .seconds(120)) } catch { break }
            }
        }
    }

    private func group(_ name: String, id: String, icon: String, tint: Color, now: Date) -> some View {
        let reading = readings[id]
        let windows = (reading?.windows ?? []).filter { ($0.reset ?? .infinity) > now.timeIntervalSince1970 }
        var summary = windows.map { window in
            var text = "\(window.label) limit: \(Int(window.percent.rounded()))% used"
            if let left = window.countdown(at: now) { text += ", resets in " + left }
            return text
        }.joined(separator: "\n")
        if !summary.isEmpty, let measured = reading?.measured {
            let time = Date(timeIntervalSince1970: measured).formatted(date: .omitted, time: .shortened)
            summary += "\nLast reading at " + time
        }
        let hint = failed ? "Couldn’t refresh account usage." : reading?.hint ?? "Waiting for usage data."
        return HStack(spacing: 6) {
            Image(systemName: icon).foregroundStyle(tint)
            Text(name).fontWeight(.medium)
            if let primary = windows.first {
                let fraction = min(1, max(0, primary.percent / 100))
                Capsule().fill(.quaternary).frame(width: 35, height: 5)
                    .overlay(alignment: .leading) {
                        Capsule().fill(fraction >= 0.9 ? Color.orange : Color.secondary)
                            .frame(width: 35 * fraction, height: 5)
                    }
                ForEach(windows.indices, id: \.self) { index in
                    if index != 0 { Text("·") }
                    Text("\(Int(windows[index].percent.rounded()))%").foregroundStyle(.primary)
                    if let left = windows[index].countdown(at: now) { Text(left) }
                }
            } else {
                if id == "claude" && (reading != nil || failed) {
                    Label("View usage", systemImage: "arrow.up.right").foregroundStyle(.link)
                } else {
                    Text("—")
                }
            }
        }
        .monospacedDigit().fixedSize()
        .help((summary.isEmpty ? hint : summary) + (id == "claude" ? "\nOpen Claude account usage in your browser." : ""))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(name + " usage. " + (summary.isEmpty ? hint : summary))
    }

    @MainActor private func refresh() async {
        guard !loading else { return }
        loading = true
        defer { loading = false }
        do {
            let data = try await Bridge.run(["usage"])
            readings = try JSONDecoder().decode([String: ProviderUsage].self, from: data)
            failed = false
        } catch {
            readings = [:]
            failed = true
        }
    }
}
