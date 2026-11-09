import AppKit
import UserNotifications
import SwiftUI
import OSLog

@MainActor
final class AttentionNotifications: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate, ObservableObject {
    @Published var destination: String?
    let store = Store()
    private let center = UNUserNotificationCenter.current()
    private let defaultsKey = "presentedAttentionRequests"
    private let log = Logger(subsystem: "local.muxteroid.app", category: "notifications")

    func applicationWillFinishLaunching(_ notification: Notification) {
        center.delegate = self
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
                if store.loadError.isEmpty { await update(store.rows) }
                try? await Task.sleep(nanoseconds: 3_000_000_000)
            }
        }
    }

    private func update(_ streams: [Stream]) async {
        var presented = Set(UserDefaults.standard.stringArray(forKey: defaultsKey) ?? [])
        var requests: [String: (Stream, String)] = [:]
        for stream in streams where stream.state != "Done" && stream.live {
            if stream.state == "Needs you" {
                requests[stream.id] = (stream, stream.detail)
            }
            for agent in stream.agents where agent.state == "Needs you" {
                requests[stream.id + "/" + agent.id] = (stream, agent.title + " needs your attention.")
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
                guard let (stream, reason) = requests[id] else { continue }
                let content = UNMutableNotificationContent()
                content.title = stream.title
                content.subtitle = "Needs you"
                content.body = reason
                content.threadIdentifier = stream.id
                content.userInfo = ["stream": stream.id]
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
            destination = id
            NSApp.activate(ignoringOtherApps: true)
        }
    }
}
