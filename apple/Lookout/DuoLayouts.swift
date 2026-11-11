import SwiftUI

// The iPhone Duo's inner-screen home layouts (picked by the owner on the design canvas,
// 2026-11-10): open flat = Focus with the big camera ending at the fold; laptop fold = live on
// top, a timeline deck below; book fold = live on the left page, cameras and events on the right.

// MARK: Today's events

/// Someone seen at a camera (a Frigate visitor), for the Today lists and the timeline.
struct CameraEvent: Identifiable, Equatable {
    let visitor: Visitor
    let camera: HubCamera
    /// From the hub's events list: has the clip (nil from older hubs' visitors list)
    var event: HubEvent? = nil

    var id: String { "\(camera.id)/\(visitor.id)" }
    var date: Date { visitor.date }
    var length: TimeInterval? { visitor.end.map { max(0, $0 - visitor.start) } }

    var lengthText: String? {
        guard let length else { return nil }
        let seconds = Int(length.rounded())
        if seconds < 60 { return seconds == 1 ? "1 second" : "\(seconds) seconds" }
        let minutes = (seconds + 30) / 60
        return minutes == 1 ? "1 minute" : "\(minutes) minutes"
    }
}

/// Today's person events across the cameras, newest first. Reloaded every minute while shown.
@MainActor
final class TodayEvents: ObservableObject {
    @Published private(set) var events: [CameraEvent] = []

    func run(_ cameras: [HubCamera], client: HubClient) async {
        while !Task.isCancelled {
            await load(cameras, client: client)
            try? await Task.sleep(nanoseconds: 60_000_000_000)
        }
    }

    private func load(_ cameras: [HubCamera], client: HubClient) async {
        var found: [CameraEvent] = []
        let byID = Dictionary(uniqueKeysWithValues: cameras.map { ($0.id, $0) })
        // One call on hubs with the events list; older ones are asked camera by camera
        if let page = try? await client.events(limit: 60) {
            found = page.events.filter { Calendar.current.isDateInToday($0.date) }
                .compactMap { e in byID[e.camera].map { CameraEvent(visitor: e.visitor, camera: $0, event: e) } }
            if found != events { events = found }
            return
        }
        for cam in cameras {
            var visitors: [Visitor] = []
            #if DEBUG
            if Demo.isDemo(cam) { visitors = Demo.visitors(cam) }
            #endif
            if visitors.isEmpty, let list = try? await client.visitors(cam, limit: 30) { visitors = list }
            found += visitors.filter { Calendar.current.isDateInToday($0.date) }.map { CameraEvent(visitor: $0, camera: cam) }
        }
        found.sort { $0.date > $1.date }
        if found != events { events = found }
    }

    func at(_ camera: String) -> [CameraEvent] { events.filter { $0.camera.id == camera } }
}

/// "Someone at Front Door" / "7:13 PM · 42 seconds", with the snapshot.
struct EventRow: View {
    let event: CameraEvent
    let client: HubClient
    var showsCamera = true

    var body: some View {
        HStack(spacing: 12) {
            EventThumb(event: event, client: client)
                .frame(width: 64, height: 48)
                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text(showsCamera ? "Someone at \(event.camera.name)" : "Person")
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(1)
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }

    private var detail: String {
        let time = event.date.formatted(date: .omitted, time: .shortened)
        return event.lengthText.map { "\(time) · \($0)" } ?? time
    }
}

/// The event's snapshot from the hub, or a plain person symbol.
struct EventThumb: View {
    let event: CameraEvent
    let client: HubClient
    @State private var image: UIImage?

    var body: some View {
        ZStack {
            Color(white: 0.16)
            if let image {
                Image(uiImage: image).resizable().scaledToFill()
            } else {
                Image(systemName: "figure.stand").foregroundStyle(.secondary)
            }
        }
        .task(id: event.id) {
            guard let path = event.visitor.thumbnail,
                  let data = try? await client.image(path, width: 240) else { return }
            image = UIImage(data: data)
        }
    }
}

/// A list of events (or a quiet line when there are none), each opening its detail.
struct EventList: View {
    let events: [CameraEvent]
    let client: HubClient
    var showsCamera = true
    var limit = 6
    @State private var open: CameraEvent?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if events.isEmpty {
                Text("No one seen today")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .padding(.vertical, 4)
            }
            ForEach(events.prefix(limit)) { event in
                Button { open = event } label: { EventRow(event: event, client: client, showsCamera: showsCamera) }
                    .buttonStyle(.plain)
            }
        }
        .sheet(item: $open) { event in
            EventDetail(event: event, client: client)
        }
    }
}

// MARK: Live pane

/// The selected camera, big and live, with its name and status on glass; tap for full screen.
/// Shares one LiveModel across postures, so folding the Duo doesn't restart the stream.
struct DuoLivePane: View {
    let camera: HubCamera
    @ObservedObject var model: LiveModel
    var cornerRadius: CGFloat = 0
    let open: () -> Void

    var body: some View {
        ZStack {
            PlayerSurface(model: model, fill: true)
            if !camera.connected {
                VStack(spacing: 8) {
                    Image(systemName: "video.slash.fill").font(.title2)
                    Text("Offline").font(.subheadline.weight(.semibold))
                }
                .foregroundStyle(.secondary)
            }
        }
        .overlay(alignment: .topLeading) {
            VStack(alignment: .leading, spacing: 2) {
                Text(camera.name).font(.headline).lineLimit(1)
                PersonStatus(camera: camera).foregroundStyle(.secondary)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 6)
            .glassEffect(.regular, in: .capsule)
            .padding(12)
        }
        .clipShape(RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
        .contentShape(Rectangle())
        .onTapGesture(perform: open)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(CameraTile.label(camera))
        .accessibilityHint("Opens full screen")
        .accessibilityAddTraits(.isButton)
    }
}

/// Snapshot and sound for the live pane (plus the camera's own controls where there's room).
struct LivePaneButtons: View {
    @ObservedObject var model: LiveModel
    @State private var saved = false
    @State private var denied = false

    var body: some View {
        HStack(spacing: 10) {
            RoundButton(symbol: "camera", label: "Save Snapshot") {
                guard let image = model.snapshot() else { return }
                Task {
                    switch await SnapshotSaver.save(image) {
                    case .saved: saved.toggle()
                    case .denied: denied = true
                    case .failed: break
                    }
                }
            }
            .disabled(model.still == nil && !model.video)
            RoundButton(symbol: model.muted ? "speaker.slash.fill" : "speaker.wave.2.fill",
                        label: model.muted ? "Turn Sound On" : "Turn Sound Off") {
                model.muted.toggle()
                PlayerAudio.start(muted: model.muted)
            }
            .disabled(!model.hasAudio)
        }
        .sensoryFeedback(.success, trigger: saved)
        .alert("Lookout can't add photos", isPresented: $denied) {
            Button("Open Settings") {
                if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Allow Lookout to add photos in Settings to save snapshots.")
        }
    }
}

/// Camera names as chips; the selected one filled.
struct CameraChips: View {
    let cameras: [HubCamera]
    @Binding var selected: String?

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(cameras) { cam in
                    let on = cam.id == selected
                    Button { selected = cam.id } label: {
                        HStack(spacing: 6) {
                            if cam.person { Circle().fill(.green).frame(width: 7, height: 7) }
                            Text(cam.name).font(.subheadline.weight(.semibold)).lineLimit(1)
                        }
                        .padding(.horizontal, 14)
                        .frame(height: 36)
                        .foregroundStyle(on ? Color.black : Color.primary)
                        .background(Capsule().fill(on ? Color.white : Color.clear))
                    }
                    .buttonStyle(.plain)
                    .glassEffect(.regular.interactive(), in: .capsule)
                    .accessibilityAddTraits(on ? .isSelected : [])
                }
            }
            .padding(.vertical, 2)
        }
    }
}

// MARK: Open flat, upright

/// Focus on the open Duo: the selected camera fills the top page down to the fold; the camera
/// strip and Today's events sit on the bottom page.
struct DuoOpenFocus: View {
    let cameras: [HubCamera]
    let client: HubClient
    let live: Bool
    @Binding var focusedID: String?
    @ObservedObject var model: LiveModel
    @ObservedObject var today: TodayEvents
    let open: (HubCamera) -> Void

    private var focused: HubCamera? { cameras.first { $0.id == focusedID } ?? cameras.first }

    var body: some View {
        GeometryReader { geo in
            let fold = geo.fold()
            let topHeight = max(200, (fold?.frame.minY ?? geo.size.height * 0.5) - 8)
            VStack(spacing: 0) {
                if let cam = focused {
                    DuoLivePane(camera: cam, model: model, cornerRadius: 18) { open(cam) }
                        .frame(height: topHeight)
                        .padding(.horizontal, 16)
                }
                Color.clear.frame(height: fold.map { $0.frame.height + 16 } ?? 16)
                ScrollView {
                    VStack(alignment: .leading, spacing: 20) {
                        strip
                        VStack(alignment: .leading, spacing: 10) {
                            Text("Today").font(.title3.weight(.bold))
                            EventList(events: today.events, client: client)
                        }
                    }
                    .padding(.horizontal, 16)
                    .padding(.bottom, 16)
                }
            }
        }
    }

    private var strip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 10) {
                ForEach(cameras) { cam in
                    let on = cam.id == focused?.id
                    Button { focusedID = cam.id } label: {
                        CameraTile(camera: cam, client: client, live: live, frameWidth: 320,
                                   transport: .mjpeg(fps: 2), compact: true, cornerRadius: 12)
                            .frame(width: 168, height: 95)
                            .overlay {
                                RoundedRectangle(cornerRadius: 12, style: .continuous)
                                    .strokeBorder(on ? Color.white : Color.clear, lineWidth: 3)
                            }
                    }
                    .buttonStyle(.plain)
                    .accessibilityAddTraits(on ? .isSelected : [])
                }
            }
            .padding(.vertical, 2)
        }
    }
}

// MARK: Laptop fold

/// Half open like a laptop: the live camera fills the top half; the bottom half is a deck with
/// the cameras, snapshot and sound, Today's timeline and the latest visitors.
struct DuoLaptop: View {
    let cameras: [HubCamera]
    let client: HubClient
    @Binding var focusedID: String?
    @ObservedObject var model: LiveModel
    @ObservedObject var today: TodayEvents
    let open: (HubCamera) -> Void
    @State private var openEvent: CameraEvent?

    private var focused: HubCamera? { cameras.first { $0.id == focusedID } ?? cameras.first }

    var body: some View {
        GeometryReader { geo in
            let fold = geo.fold()
            let top = (fold?.frame.minY ?? geo.size.height / 2)
            let deckTop = (fold?.frame.maxY ?? geo.size.height / 2)
            VStack(spacing: 0) {
                if let cam = focused {
                    DuoLivePane(camera: cam, model: model) { open(cam) }
                        .frame(height: max(0, top + geo.safeAreaInsets.top))
                        .ignoresSafeArea(edges: .top)
                        .padding(.top, -geo.safeAreaInsets.top)
                }
                Color.clear.frame(height: max(0, deckTop - top))
                deck
                    .frame(maxHeight: .infinity, alignment: .top)
            }
        }
        .sheet(item: $openEvent) { event in
            EventDetail(event: event, client: client)
        }
    }

    private var deck: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                HStack(spacing: 12) {
                    CameraChips(cameras: cameras, selected: $focusedID)
                    LivePaneButtons(model: model)
                }
                VStack(alignment: .leading, spacing: 10) {
                    HStack(alignment: .firstTextBaseline) {
                        Text("Today").font(.title3.weight(.bold))
                        Text(summary).font(.subheadline).foregroundStyle(.secondary)
                    }
                    DayTimeline(events: today.events) { openEvent = $0 }
                }
                clips
            }
            .padding(16)
        }
    }

    private var summary: String {
        let n = today.events.count
        return n == 0 ? "no one seen" : n == 1 ? "1 person" : "\(n) people"
    }

    @ViewBuilder private var clips: some View {
        if !today.events.isEmpty {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 12) {
                    ForEach(today.events.prefix(6)) { event in
                        Button { openEvent = event } label: { ClipCard(event: event, client: client) }
                            .buttonStyle(.plain)
                    }
                }
            }
        }
    }
}

/// One visitor as a card: snapshot, time, camera and how long.
private struct ClipCard: View {
    let event: CameraEvent
    let client: HubClient

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            EventThumb(event: event, client: client)
                .frame(width: 176, height: 99)
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            Text(event.date, style: .time).font(.subheadline.weight(.semibold))
            Text([event.camera.name, event.lengthText].compactMap { $0 }.joined(separator: " · "))
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .frame(width: 176, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

/// Today from 6 AM (earlier if someone came before then) to now, with a mark for each person event.
struct DayTimeline: View {
    let events: [CameraEvent]
    let select: (CameraEvent) -> Void

    var body: some View {
        TimelineView(.periodic(from: .now, by: 60)) { context in
            let now = context.date
            let midnight = Calendar.current.startOfDay(for: now)
            let first = events.map(\.date).filter { $0 >= midnight }.min() ?? now
            let earliest = min(midnight.addingTimeInterval(6 * 3600), first, now.addingTimeInterval(-3600))
            // Back to the whole hour before the earliest moment shown
            let start = max(midnight, Calendar.current.dateInterval(of: .hour, for: earliest)?.start ?? earliest)
            let span = max(1, now.timeIntervalSince(start))
            VStack(spacing: 6) {
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Capsule().fill(Color(white: 0.18)).frame(height: 28)
                        ForEach(events.filter { $0.date >= start }) { event in
                            let x = geo.size.width * event.date.timeIntervalSince(start) / span
                            Button { select(event) } label: {
                                Capsule().fill(.green)
                                    .frame(width: 6, height: 20)
                                    .frame(width: 30, height: 36)
                                    .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .position(x: min(max(x, 3), geo.size.width - 3), y: geo.size.height / 2)
                            .accessibilityLabel("Someone at \(event.camera.name), \(event.date.formatted(date: .omitted, time: .shortened))")
                        }
                    }
                }
                .frame(height: 36)
                HStack {
                    Text(start, format: .dateTime.hour().minute())
                    Spacer()
                    Text(start.addingTimeInterval(span / 2), format: .dateTime.hour().minute())
                    Spacer()
                    Text("Now")
                }
                .font(.caption2)
                .foregroundStyle(.secondary)
                .accessibilityHidden(true)
            }
        }
    }
}

// MARK: Book fold

/// Half open like a book (or flat and sideways): the live camera fills the left page; the right
/// page has the cameras and Today's events at the selected one.
struct DuoBook: View {
    let cameras: [HubCamera]
    let client: HubClient
    let live: Bool
    @Binding var focusedID: String?
    @ObservedObject var model: LiveModel
    @ObservedObject var today: TodayEvents
    let open: (HubCamera) -> Void
    let showWall: () -> Void

    private var focused: HubCamera? { cameras.first { $0.id == focusedID } ?? cameras.first }

    var body: some View {
        GeometryReader { geo in
            let fold = geo.fold()
            let left = fold?.frame.minX ?? geo.size.width / 2
            let right = fold?.frame.maxX ?? geo.size.width / 2
            HStack(spacing: 0) {
                if let cam = focused {
                    DuoLivePane(camera: cam, model: model) { open(cam) }
                        .overlay(alignment: .bottomLeading) {
                            LivePaneButtons(model: model).padding(12)
                        }
                        .frame(width: max(0, left + geo.safeAreaInsets.leading))
                        .padding(.leading, -geo.safeAreaInsets.leading)
                        .ignoresSafeArea(edges: [.top, .bottom])
                }
                Color.clear.frame(width: max(0, right - left))
                page
                    .frame(maxWidth: .infinity)
            }
        }
    }

    private var page: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text("Cameras").font(.title3.weight(.bold))
                LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)], spacing: 10) {
                    ForEach(cameras) { cam in
                        let on = cam.id == focused?.id
                        Button { focusedID = cam.id } label: {
                            CameraTile(camera: cam, client: client, live: live, frameWidth: 320,
                                       transport: .mjpeg(fps: 2), compact: true, cornerRadius: 12)
                                .aspectRatio(16 / 9, contentMode: .fit)
                                .overlay {
                                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                                        .strokeBorder(on ? Color.white : Color.clear, lineWidth: 3)
                                }
                        }
                        .buttonStyle(.plain)
                        .accessibilityAddTraits(on ? .isSelected : [])
                    }
                }
                if let cam = focused {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("Today at \(cam.name)").font(.headline)
                        EventList(events: today.at(cam.id), client: client, showsCamera: false, limit: 4)
                    }
                }
                if cameras.count > 1 {
                    Button(action: showWall) {
                        Label("Wall View", systemImage: "square.grid.2x2")
                            .font(.subheadline.weight(.semibold))
                            .padding(.horizontal, 16)
                            .frame(height: 40)
                    }
                    .buttonStyle(.plain)
                    .glassEffect(.regular.interactive(), in: .capsule)
                }
            }
            .padding(16)
        }
    }
}
