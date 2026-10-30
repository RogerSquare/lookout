import SwiftUI
import WidgetKit

/// Front-door complication / Smart Stack widget: the latest frame from the door camera and
/// whether someone is there. Refreshed every 15 minutes, every 2 while a person is in view, and
/// right away when a person alert reaches the watch (the app reloads the timeline).
@main
struct LookoutWidgets: WidgetBundle {
    var body: some Widget {
        DoorWidget()
    }
}

struct DoorWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "LookoutDoor", provider: DoorProvider()) { entry in
            DoorWidgetView(entry: entry)
                .containerBackground(.black, for: .widget)
                .widgetURL(URL(string: "lookout://camera/\(entry.cameraID)"))
        }
        .configurationDisplayName("Front Door")
        .description("The door camera, and whether someone is there.")
        .supportedFamilies([.accessoryRectangular, .accessoryCircular, .accessoryCorner, .accessoryInline])
    }
}

struct DoorEntry: TimelineEntry {
    var date = Date()
    var cameraID = "kitchen"
    var name = "Front Door"
    var person = false
    var lastSeen: Date?
    var offline = false
    var image: UIImage?

    static let placeholder = DoorEntry(name: "Kitchen Cam")
}

struct DoorProvider: TimelineProvider {
    func placeholder(in context: Context) -> DoorEntry { .placeholder }

    func getSnapshot(in context: Context, completion: @escaping (DoorEntry) -> Void) {
        if context.isPreview {
            completion(.placeholder)
            return
        }
        Task { completion(await Self.load()) }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<DoorEntry>) -> Void) {
        Task {
            let entry = await Self.load()
            let minutes: Double = entry.person ? 2 : 15
            completion(Timeline(entries: [entry], policy: .after(Date().addingTimeInterval(minutes * 60))))
        }
    }

    /// The door is the first camera the hub lists (the user's order lives in the app, not here).
    static func load() async -> DoorEntry {
        HubSession.shared.reload()            // the watch app may have paired since this process started
        let client = HubClient()
        guard let cam = try? await client.cameras().cameras.first else {
            return DoorEntry(offline: true)
        }
        var entry = DoorEntry(cameraID: cam.id, name: cam.name, person: cam.person,
                              lastSeen: cam.lastSeenDate, offline: !cam.connected)
        if let data = try? await client.image(cam.frame, width: 240) {
            entry.image = UIImage(data: data)
        }
        return entry
    }
}

struct DoorWidgetView: View {
    let entry: DoorEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        switch family {
        case .accessoryRectangular: rectangular
        case .accessoryCircular: circular
        case .accessoryCorner: corner
        default: inline
        }
    }

    /// Smart Stack: picture on the left, name and status on the right.
    private var rectangular: some View {
        HStack(spacing: 8) {
            ZStack {
                Color(white: 0.15)
                if let image = entry.image {
                    Image(uiImage: image).resizable().aspectRatio(contentMode: .fill)
                } else {
                    Image(systemName: "video.fill").foregroundStyle(.secondary)
                }
            }
            .frame(width: 72, height: 46)
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous)
                .strokeBorder(entry.person ? Color.green : .white.opacity(0.15), lineWidth: entry.person ? 2 : 1))

            VStack(alignment: .leading, spacing: 1) {
                Text(entry.name).font(.headline).lineLimit(1)
                status.font(.caption2).lineLimit(1)
            }
            Spacer(minLength: 0)
        }
    }

    private var circular: some View {
        ZStack {
            AccessoryWidgetBackground()
            Image(systemName: entry.person ? "figure.stand" : "video.fill")
                .font(.title3.weight(.semibold))
                .foregroundStyle(entry.person ? Color.green : .primary)
                .widgetAccentable()
        }
    }

    private var corner: some View {
        Image(systemName: entry.person ? "figure.stand" : "video.fill")
            .font(.title2.weight(.semibold))
            .foregroundStyle(entry.person ? Color.green : .primary)
            .widgetAccentable()
            .widgetLabel { status }
    }

    private var inline: some View {
        HStack(spacing: 4) {
            Image(systemName: entry.person ? "figure.stand" : "video.fill")
            status
        }
    }

    @ViewBuilder private var status: some View {
        if entry.offline {
            Text("Offline").foregroundStyle(.secondary)
        } else if entry.person {
            Text("Person now").foregroundStyle(.green).widgetAccentable()
        } else if let seen = entry.lastSeen {
            Text("Seen \(seen, style: .relative) ago").foregroundStyle(.secondary)
        } else {
            Text("No one seen").foregroundStyle(.secondary)
        }
    }
}
