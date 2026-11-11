import UserNotifications

/// Adds the detection snapshot to a person alert (like the Apple Watch's alert): the hub's push
/// names it ("snapshot"), this fetches it over the paired, signed connection and attaches it.
final class NotificationService: UNNotificationServiceExtension {
    private var content: UNMutableNotificationContent?
    private var deliver: ((UNNotificationContent) -> Void)?

    override func didReceive(_ request: UNNotificationRequest,
                             withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void) {
        let content = (request.content.mutableCopy() as? UNMutableNotificationContent) ?? UNMutableNotificationContent()
        self.content = content
        deliver = contentHandler
        guard let path = request.content.userInfo["snapshot"] as? String else {
            contentHandler(content)
            return
        }
        Task {
            HubSession.shared.reload()
            if let data = try? await HubClient().image(path, width: 1000) {
                let url = FileManager.default.temporaryDirectory
                    .appendingPathComponent(UUID().uuidString).appendingPathExtension("jpg")
                if (try? data.write(to: url)) != nil,
                   let attachment = try? UNNotificationAttachment(identifier: "snapshot", url: url) {
                    content.attachments = [attachment]
                }
            }
            finish()
        }
    }

    /// Out of time: show the alert without the picture rather than not at all
    override func serviceExtensionTimeWillExpire() {
        finish()
    }

    private func finish() {
        guard let deliver, let content else { return }
        self.deliver = nil
        deliver(content)
    }
}
