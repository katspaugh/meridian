import AppKit
import UserNotifications
import SwiftUI
import OSLog

@MainActor
final class AttentionNotifications: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate, ObservableObject {
    @Published var destination: String?
    var terminalDestination: String?
    let store = Store()
    let manager = ManagerStore()
    /// The workstream terminal on screen; sweeps never type into it while the app is active.
    var focusedTerminal: String? { didSet { reportFocus() } }
    private var reportedFocus: String?
    private var focusReport: Task<Void, Never>?
    private let center = UNUserNotificationCenter.current()
    private let defaultsKey = "presentedAttentionRequests"
    private let log = Logger(subsystem: "local.muxteroid.app", category: "notifications")
    private var lastPipelinePoll = Date.distantPast
    private var pipelinePollRunning = false

    func applicationWillFinishLaunching(_ notification: Notification) {
        center.delegate = self
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        // Workstreams are persisted and their dtach processes outlive this UI.
        // Quitting is not a request to close any terminal sessions.
        .terminateNow
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        reportFocus()
        Task { _ = try? await Bridge.run(["manager-active", "1"]) }
    }
    func applicationDidResignActive(_ notification: Notification) {
        reportFocus()
        Task { _ = try? await Bridge.run(["manager-active", "0"]) }
    }

    /// A sweep consults for minutes, so the bridge re-reads this just before it types.
    private func reportFocus() {
        let id = NSApp.isActive ? focusedTerminal ?? "" : ""
        guard id != reportedFocus else { return }
        reportedFocus = id
        // Reports run in order, so an older one never lands last.
        let previous = focusReport
        focusReport = Task {
            await previous?.value
            _ = try? await Bridge.run(["manager-focus", id])
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        Task {
            do {
                _ = try await center.requestAuthorization(options: [.alert, .sound])
            } catch {
                log.error("Unable to request notification permission: \(error.localizedDescription)")
            }
        }
        Task {
            while !Task.isCancelled {
                await store.refresh()
                if store.loadError.isEmpty {
                    await update(store.rows)
                    pollPipelines()
                }
                // A sweep can take minutes, so it runs beside the loop rather than inside it.
                if await manager.refresh(), manager.state?.sweepDue == true, !manager.sweeping {
                    let exclude = NSApp.isActive ? focusedTerminal : nil
                    Task { await manager.sweep(excluding: exclude) }
                }
                try? await Task.sleep(nanoseconds: 3_000_000_000)
            }
        }
    }

    /// Asks the bridge to read the pull requests of runs that wait on GitHub, about once a minute.
    /// The poll runs on its own, so a slow `gh` never delays the refresh, and never overlaps the last one.
    private func pollPipelines() {
        let ids = store.rows.filter { $0.pipeline?.polling == true }.map(\.id)
        guard !ids.isEmpty, !pipelinePollRunning, Date().timeIntervalSince(lastPipelinePoll) >= 60 else { return }
        lastPipelinePoll = Date()
        pipelinePollRunning = true
        Task {
            defer { pipelinePollRunning = false }
            do {
                _ = try await Bridge.run(["pipeline-poll"] + ids)
            } catch {
                log.error("Unable to poll pull requests: \(error.localizedDescription)")
            }
        }
    }

    private func update(_ streams: [Stream]) async {
        var presented = Set(UserDefaults.standard.stringArray(forKey: defaultsKey) ?? [])
        var requests: [String: (Stream, String, String)] = [:]
        for stream in streams where stream.state != "Done" && stream.live {
            for terminal in stream.terminals where terminal.live {
                if terminal.state == "Needs you" {
                    requests[terminal.id] = (stream, terminal.title + ": " + terminal.detail, terminal.id)
                }
                for agent in terminal.agents where agent.state == "Needs you" {
                    requests[terminal.id + "/" + agent.id] = (stream, agent.title + " needs your attention.", terminal.id)
                }
            }
        }
        let cleared = presented.subtracting(requests.keys)
        if !cleared.isEmpty {
            center.removePendingNotificationRequests(withIdentifiers: Array(cleared))
            center.removeDeliveredNotifications(withIdentifiers: Array(cleared))
            presented.subtract(cleared)
        }
        let new = Set(requests.keys).subtracting(presented)
        if !new.isEmpty, await center.notificationSettings().authorizationStatus == .authorized {
            for id in new.sorted() {
                guard let (stream, reason, terminalID) = requests[id] else { continue }
                let content = UNMutableNotificationContent()
                content.title = stream.title
                content.subtitle = "Needs you"
                content.body = reason
                content.threadIdentifier = stream.id
                content.userInfo = ["stream": stream.id, "terminal": terminalID]
                content.sound = .default
                do {
                    try await center.add(UNNotificationRequest(identifier: id, content: content, trigger: nil))
                    presented.insert(id)
                } catch {
                    log.error("Unable to deliver attention alert: \(error.localizedDescription)")
                }
            }
        }
        let saved = presented.sorted()
        if saved != (UserDefaults.standard.stringArray(forKey: defaultsKey) ?? []) {
            UserDefaults.standard.set(saved, forKey: defaultsKey)
        }
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
        willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        [.banner, .list, .sound]
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse) async {
        guard response.actionIdentifier == UNNotificationDefaultActionIdentifier,
              let id = response.notification.request.content.userInfo["stream"] as? String else { return }
        await MainActor.run {
            terminalDestination = response.notification.request.content.userInfo["terminal"] as? String
            destination = id
            NSApp.activate(ignoringOtherApps: true)
        }
    }
}
