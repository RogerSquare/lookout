import ActivityKit
import AppIntents
import SwiftUI
import WidgetKit

@main
struct LookoutWidgetsBundle: WidgetBundle {
    var body: some Widget {
        CamerasWidget()
        PersonLiveActivity()
        SleepAllControl()
        OpenDoorControl()
    }
}

// MARK: - Cameras widget (Home Screen and Lock Screen)

struct CameraSnap: Identifiable {
    let id: String
    let name: String
    let person: Bool
    let asleep: Bool
    let offline: Bool
    let lastSeen: Date?
    var image: UIImage?
}

struct CamerasEntry: TimelineEntry {
    var date = Date()
    var cameras: [CameraSnap] = []
    /// Couldn't reach the hub (or not paired): said plainly instead of a stale picture
    var problem: String?

    static let placeholder = CamerasEntry(cameras: [
        CameraSnap(id: "front_door", name: "Front Door", person: false, asleep: false, offline: false, lastSeen: nil),
        CameraSnap(id: "bedroom", name: "Bedroom", person: false, asleep: false, offline: false, lastSeen: nil)])
}

struct CamerasProvider: TimelineProvider {
    func placeholder(in context: Context) -> CamerasEntry { .placeholder }

    func getSnapshot(in context: Context, completion: @escaping (CamerasEntry) -> Void) {
        if context.isPreview { completion(.placeholder); return }
        Task { completion(await Self.load(max: Self.count(context.family))) }
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<CamerasEntry>) -> Void) {
        Task {
            let entry = await Self.load(max: Self.count(context.family))
            // Every 15 minutes; every 2 while someone is in view (the system may stretch this)
            let minutes: Double = entry.cameras.contains { $0.person } ? 2 : 15
            completion(Timeline(entries: [entry], policy: .after(Date().addingTimeInterval(minutes * 60))))
        }
    }

    static func count(_ family: WidgetFamily) -> Int {
        switch family {
        case .systemMedium: return 2
        case .systemLarge, .systemExtraLarge: return 4
        default: return 1
        }
    }

    /// Door cameras first, then the hub's order.
    static func load(max: Int) async -> CamerasEntry {
        HubSession.shared.reload()
        guard HubSession.shared.link != nil else { return CamerasEntry(problem: "Open Lookout to pair with your hub") }
        let client = HubClient()
        guard let list = try? await client.cameras().cameras else { return CamerasEntry(problem: "Can't reach your hub") }
        let ordered = list.filter { $0.role == "door" } + list.filter { $0.role != "door" }
        var snaps: [CameraSnap] = []
        for cam in ordered.prefix(max) {
            var snap = CameraSnap(id: cam.id, name: cam.name, person: cam.person, asleep: cam.isAsleep,
                                  offline: !cam.connected && !cam.isAsleep, lastSeen: cam.lastSeenDate)
            if cam.connected, !cam.isAsleep, let data = try? await client.image(cam.frame, width: max > 1 ? 400 : 500) {
                snap.image = UIImage(data: data)
            }
            snaps.append(snap)
        }
        return CamerasEntry(cameras: snaps)
    }
}

struct CamerasWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: "LookoutCameras", provider: CamerasProvider()) { entry in
            CamerasWidgetView(entry: entry)
                .containerBackground(.black, for: .widget)
        }
        .configurationDisplayName("Cameras")
        .description("The latest picture from your cameras, and whether someone is there.")
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge,
                            .accessoryRectangular, .accessoryCircular, .accessoryInline])
        .contentMarginsDisabled()
    }
}

struct CamerasWidgetView: View {
    let entry: CamerasEntry
    @Environment(\.widgetFamily) private var family

    var body: some View {
        if let problem = entry.problem {
            problemView(problem)
        } else {
            switch family {
            case .accessoryRectangular: rectangular
            case .accessoryCircular: circular
            case .accessoryInline: inline
            case .systemSmall: if let cam = entry.cameras.first { tile(cam).widgetURL(url(cam)) }
            case .systemMedium:
                HStack(spacing: 4) { ForEach(entry.cameras) { cam in Link(destination: url(cam)) { tile(cam) } } }
                    .padding(4)
            default:
                let rows = stride(from: 0, to: entry.cameras.count, by: 2).map { Array(entry.cameras[$0..<min($0 + 2, entry.cameras.count)]) }
                VStack(spacing: 4) {
                    ForEach(rows.indices, id: \.self) { i in
                        HStack(spacing: 4) { ForEach(rows[i]) { cam in Link(destination: url(cam)) { tile(cam) } } }
                    }
                }
                .padding(4)
            }
        }
    }

    private func url(_ cam: CameraSnap) -> URL { URL(string: "lookout://camera/\(cam.id)")! }

    private func tile(_ cam: CameraSnap) -> some View {
        ZStack(alignment: .bottomLeading) {
            Color(white: 0.12)
            if let image = cam.image {
                Image(uiImage: image).resizable().aspectRatio(contentMode: .fill)
            } else {
                Image(systemName: cam.asleep ? "moon.zzz.fill" : (cam.offline ? "video.slash.fill" : "video.fill"))
                    .font(.title2)
                    .foregroundStyle(cam.asleep ? Color.indigo : Color.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            LinearGradient(colors: [.clear, .black.opacity(0.7)], startPoint: .center, endPoint: .bottom)
            VStack(alignment: .leading, spacing: 1) {
                Text(cam.name).font(.caption.weight(.semibold)).lineLimit(1)
                status(cam).font(.caption2).lineLimit(1)
            }
            .padding(8)
        }
        .clipShape(ContainerRelativeShape())
        .overlay(ContainerRelativeShape().strokeBorder(cam.person ? Color.green : .clear, lineWidth: 3))
    }

    @ViewBuilder private func status(_ cam: CameraSnap) -> some View {
        if cam.person {
            Text("Person now").foregroundStyle(.green)
        } else if cam.asleep {
            Text("Asleep").foregroundStyle(.secondary)
        } else if cam.offline {
            Text("Offline").foregroundStyle(.secondary)
        } else if let seen = cam.lastSeen {
            Text("Seen \(seen, style: .relative) ago").foregroundStyle(.secondary)
        } else {
            Text("No one seen").foregroundStyle(.secondary)
        }
    }

    private func problemView(_ text: String) -> some View {
        VStack(spacing: 6) {
            Image(systemName: "wifi.exclamationmark").font(.title3)
            Text(text).font(.caption).multilineTextAlignment(.center)
        }
        .foregroundStyle(.secondary)
        .padding()
    }

    private var rectangular: some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(entry.cameras.prefix(1)) { cam in
                Label(cam.name, systemImage: cam.person ? "figure.stand" : "video.fill").font(.headline).lineLimit(1)
                status(cam).font(.caption)
            }
        }
        .widgetURL(entry.cameras.first.map(url))
    }

    private var circular: some View {
        ZStack {
            AccessoryWidgetBackground()
            Image(systemName: entry.cameras.first?.person == true ? "figure.stand" : "video.fill")
                .font(.title3.weight(.semibold))
                .widgetAccentable()
        }
        .widgetURL(entry.cameras.first.map(url))
    }

    private var inline: some View {
        HStack(spacing: 4) {
            Image(systemName: entry.cameras.first?.person == true ? "figure.stand" : "video.fill")
            if let cam = entry.cameras.first { status(cam) }
        }
    }
}

// MARK: - Live Activity: someone at the door

/// Only while someone is in view of a door camera, and as quiet as possible: a person glyph in
/// the Dynamic Island, the camera's name when it's opened or on the Lock Screen. No timer; it
/// goes away as soon as they leave.
struct PersonLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: PersonActivityAttributes.self) { context in
            // Lock Screen
            HStack(spacing: 12) {
                Image(systemName: "figure.stand")
                    .font(.title2)
                    .foregroundStyle(.green)
                Text(context.attributes.name).font(.headline)
                Spacer()
            }
            .padding(.horizontal, 18)
            .padding(.vertical, 12)
            .activityBackgroundTint(.black.opacity(0.55))
            .widgetURL(URL(string: "lookout://camera/\(context.attributes.cameraID)"))
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Image(systemName: "figure.stand").font(.title2).foregroundStyle(.green)
                }
                DynamicIslandExpandedRegion(.center) {
                    Text(context.attributes.name).font(.headline)
                }
            } compactLeading: {
                Image(systemName: "figure.stand").foregroundStyle(.green)
            } compactTrailing: {
                EmptyView()
            } minimal: {
                Image(systemName: "figure.stand").foregroundStyle(.green)
            }
            .widgetURL(URL(string: "lookout://camera/\(context.attributes.cameraID)"))
        }
    }
}

// MARK: - Control Center

/// "Sleep All Cameras": on when every camera that can sleep is asleep.
struct SleepAllControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: "LookoutSleepAll", provider: SleepAllValue()) { asleep in
            ControlWidgetToggle("Sleep All Cameras", isOn: asleep, action: SetSleepAllIntent()) { on in
                Label(on ? "Asleep" : "Awake", systemImage: on ? "moon.zzz.fill" : "video.fill")
            }
        }
        .displayName("Sleep All Cameras")
        .description("Turns every camera that can sleep off, or back on.")
    }
}

struct SleepAllValue: ControlValueProvider {
    var previewValue: Bool { false }

    func currentValue() async throws -> Bool {
        HubSession.shared.reload()
        let cams = try await HubClient().cameras().cameras.filter { $0.can("sleep") }
        return !cams.isEmpty && cams.allSatisfy(\.isAsleep)
    }
}

struct SetSleepAllIntent: SetValueIntent {
    static let title: LocalizedStringResource = "Sleep All Cameras"
    @Parameter(title: "Asleep") var value: Bool

    func perform() async throws -> some IntentResult {
        HubSession.shared.reload()
        let client = HubClient()
        for cam in try await client.cameras().cameras where cam.can("sleep") && cam.isAsleep != value {
            try await client.setAsleep(cam, value)
            try? await Task.sleep(nanoseconds: 300_000_000)
        }
        return .result()
    }
}

/// "Open Front Door": the app, showing the door camera.
struct OpenDoorControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: "LookoutOpenDoor") {
            ControlWidgetButton(action: OpenURLIntent(URL(string: "lookout://door")!)) {
                Label("Front Door", systemImage: "door.left.hand.open")
            }
        }
        .displayName("Open Front Door")
        .description("Shows the door camera in Lookout.")
    }
}
