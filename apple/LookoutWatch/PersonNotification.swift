import SwiftUI
import UserNotifications
import WatchKit
import WidgetKit

/// Long-look screen for a person alert: the frame that triggered it, on the same rounded card with
/// the green "person" edge as the app, then the title and camera name.
final class PersonNotificationController: WKUserNotificationHostingController<PersonNotificationView> {
    private var title = "Someone's at the door"
    private var subtitle = ""
    private var snapshot: String?

    override var body: PersonNotificationView {
        PersonNotificationView(title: title, subtitle: subtitle, snapshot: snapshot)
    }

    override func didReceive(_ notification: UNNotification) {
        let content = notification.request.content
        title = content.title.isEmpty ? title : content.title
        subtitle = content.subtitle
        snapshot = content.userInfo["snapshot"] as? String
        WidgetCenter.shared.reloadAllTimelines()   // complication shows "Person now" right away
        // The system reads `body` after didReceive returns
    }
}

struct PersonNotificationView: View {
    let title: String
    let subtitle: String
    let snapshot: String?
    @State private var image: UIImage?
    @State private var failed = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ZStack {
                Color(white: 0.12)
                if let image {
                    Image(uiImage: image)
                        .resizable()
                        .aspectRatio(contentMode: .fill)
                        .transition(.opacity)
                } else if failed {
                    Image(systemName: "video.slash").foregroundStyle(.secondary)
                } else {
                    ProgressView()
                }
            }
            .aspectRatio(16 / 9, contentMode: .fit)
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous)
                .strokeBorder(Color.green, lineWidth: 2))
            .shadow(color: .green.opacity(0.5), radius: 8)

            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.headline)
                if !subtitle.isEmpty {
                    Text(subtitle).font(.footnote).foregroundStyle(.secondary)
                }
            }
        }
        .task(id: snapshot) {
            guard let snapshot else { failed = true; return }
            if let data = try? await HubClient().image(snapshot, width: 400), let img = UIImage(data: data) {
                withAnimation(.easeOut(duration: 0.25)) { image = img }
            } else {
                failed = true
            }
        }
    }
}
