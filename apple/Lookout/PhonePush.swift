import SwiftUI
import UserNotifications

/// iPhone push: registers with APNs and gives the hub this app's token. The hub sends person
/// alerts to the watch app by default; the iPhone token is there for the faster route (iPhone,
/// mirrored to the watch while the phone is locked) once that's chosen.
final class PhoneAppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions options: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        Task { @MainActor in LiveActivities.start() }
        #if DEBUG
        if Demo.enabled { return true }      // demo screenshots: no permission prompt over the cameras
        #endif
        center.requestAuthorization(options: [.alert, .sound, .badge]) { granted, _ in
            if granted {
                DispatchQueue.main.async { application.registerForRemoteNotifications() }
            }
        }
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken token: Data) {
        Task {
            for delay in [0, 5, 30, 120] as [UInt64] {
                try? await Task.sleep(nanoseconds: delay * 1_000_000_000)
                if (try? await HubClient().registerPush(token: token)) != nil { return }
            }
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .sound, .list])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler done: @escaping () -> Void) {
        if let cam = response.notification.request.content.userInfo["camera"] as? String {
            AlertRouter.shared.open(cam)
        }
        done()
    }
}
