import SwiftUI
import UserNotifications
import WatchKit
import WidgetKit

/// Push alerts: asks permission, registers with APNs, hands the token to the hub, and routes a
/// tapped "person" notification to its camera.
final class AppDelegate: NSObject, WKApplicationDelegate, UNUserNotificationCenterDelegate {
    func applicationDidFinishLaunching() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound, .badge]) { granted, _ in
            if granted {
                DispatchQueue.main.async { WKApplication.shared().registerForRemoteNotifications() }
            }
        }
    }

    func didRegisterForRemoteNotifications(withDeviceToken deviceToken: Data) {
        Task {
            // The hub may be briefly unreachable (away from home): retry a few times
            for delay in [0, 5, 30, 120] as [UInt64] {
                try? await Task.sleep(nanoseconds: delay * 1_000_000_000)
                if (try? await HubClient().registerPush(token: deviceToken)) != nil { return }
            }
        }
    }

    func didFailToRegisterForRemoteNotificationsWithError(_ error: Error) {}

    // App open when an alert arrives: the page already taps and jumps to the camera, so just
    // add the sound and keep it in the notification list.
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        WidgetCenter.shared.reloadAllTimelines()
        done([.sound, .list])
    }

    // Tapped: open on that camera
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        if let cam = response.notification.request.content.userInfo["camera"] as? String {
            DispatchQueue.main.async { AlertRouter.shared.open(cam) }
        }
        done()
    }
}
