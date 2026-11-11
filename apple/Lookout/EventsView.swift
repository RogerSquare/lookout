import AVKit
import Photos
import SwiftUI

/// The Events tab: everyone the cameras saw, newest first, grouped by day, with a camera filter.
/// Tap one to play its clip.
struct EventsView: View {
    @ObservedObject var store: CameraStore
    @ObservedObject private var prefs = Prefs.shared
    @State private var events: [CameraEvent] = []
    @State private var more = true
    @State private var loading = false
    @State private var failed: String?
    @State private var camera: String?          // nil = every camera
    @State private var open: CameraEvent?

    private var cameras: [HubCamera] { prefs.sorted(store.cameras) }

    var body: some View {
        NavigationStack {
            List {
                if events.isEmpty && !loading {
                    ContentUnavailableView {
                        Label(failed == nil ? "No One Seen" : "Can't Load Events",
                              systemImage: failed == nil ? "figure.stand" : "exclamationmark.triangle")
                    } description: {
                        Text(failed ?? "People the cameras see show up here, with a clip of each visit. Event history needs Frigate connected on the hub's Detection page.")
                    }
                    .listRowBackground(Color.clear)
                }
                ForEach(days, id: \.day) { group in
                    Section(Self.title(group.day)) {
                        ForEach(group.events) { event in
                            Button { open = event } label: {
                                EventRow(event: event, client: store.client, showsCamera: camera == nil)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
                if more && !events.isEmpty {
                    ProgressView()
                        .frame(maxWidth: .infinity)
                        .listRowBackground(Color.clear)
                        .task { await loadMore() }
                }
            }
            .listStyle(.insetGrouped)
            .navigationTitle("Events")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        Picker("Camera", selection: $camera) {
                            Text("All Cameras").tag(String?.none)
                            ForEach(cameras) { cam in Text(cam.name).tag(Optional(cam.id)) }
                        }
                    } label: {
                        Image(systemName: camera == nil ? "line.3.horizontal.decrease.circle"
                                                        : "line.3.horizontal.decrease.circle.fill")
                    }
                    .accessibilityLabel("Filter by Camera")
                }
            }
            .refreshable { await reload() }
            // Again once the camera list arrives (events name cameras by id)
            .task(id: "\(camera ?? "")|\(store.cameras.map(\.id).joined(separator: ","))") { await reload() }
            .sheet(item: $open) { event in
                EventDetail(event: event, client: store.client)
            }
        }
    }

    private struct Day { let day: Date; let events: [CameraEvent] }

    private var days: [Day] {
        let cal = Calendar.current
        var out: [Day] = []
        for e in events {
            let d = cal.startOfDay(for: e.date)
            if out.last?.day == d { out[out.count - 1] = Day(day: d, events: out[out.count - 1].events + [e]) }
            else { out.append(Day(day: d, events: [e])) }
        }
        return out
    }

    private static func title(_ day: Date) -> String {
        let cal = Calendar.current
        if cal.isDateInToday(day) { return "Today" }
        if cal.isDateInYesterday(day) { return "Yesterday" }
        return day.formatted(.dateTime.weekday(.wide).month().day())
    }

    private func reload() async {
        more = true
        events = []
        await loadMore()
    }

    private func loadMore() async {
        guard !loading, more else { return }
        loading = true
        defer { loading = false }
        #if DEBUG
        if Demo.enabled {
            events = cameras.flatMap { cam in Demo.visitors(cam).map { CameraEvent(visitor: $0, camera: cam) } }
                .filter { camera == nil || $0.camera.id == camera }
                .sorted { $0.date > $1.date }
            more = false
            return
        }
        #endif
        do {
            let page = try await store.client.events(camera: camera, before: events.last?.visitor.start, limit: 30)
            let byID = Dictionary(uniqueKeysWithValues: store.cameras.map { ($0.id, $0) })
            let new = page.events.compactMap { e in byID[e.camera].map { CameraEvent(visitor: e.visitor, camera: $0, event: e) } }
            events += new.filter { n in !events.contains { $0.id == n.id } }
            more = page.more && !new.isEmpty
            failed = nil
        } catch HubError.status(404) {
            more = false
            failed = "Update your hub to see events here."
        } catch {
            more = false
            failed = error.localizedDescription
        }
    }
}

/// One event: its clip playing (or the snapshot, while it's still going on), with Share and
/// Save to Photos.
struct EventDetail: View {
    let event: CameraEvent
    let client: HubClient
    @Environment(\.dismiss) private var dismiss
    @State private var player: AVPlayer?
    @State private var file: URL?
    @State private var working = false
    @State private var message: String?
    @State private var saved = false

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 16) {
                ZStack {
                    Color.black
                    if let player {
                        VideoPlayer(player: player)
                    } else {
                        EventThumb(event: event, client: client)
                            .aspectRatio(contentMode: .fit)
                        if event.event?.clip != nil { ProgressView() }
                    }
                }
                .aspectRatio(16 / 9, contentMode: .fit)
                .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                VStack(alignment: .leading, spacing: 4) {
                    Text("Someone at \(event.camera.name)").font(.headline)
                    Text(subtitle).font(.subheadline).foregroundStyle(.secondary)
                    if event.event?.clip == nil {
                        Text(event.event == nil ? "This hub doesn't keep clips." : "The clip is ready once they've gone.")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                }
                if event.event?.clip != nil {
                    HStack(spacing: 12) {
                        if let file {
                            ShareLink(item: file) { Label("Share", systemImage: "square.and.arrow.up") }
                                .buttonStyle(.glass)
                        }
                        Button { Task { await saveToPhotos() } } label: {
                            Label("Save to Photos", systemImage: "square.and.arrow.down")
                        }
                        .buttonStyle(.glass)
                        .disabled(working)
                    }
                }
                if let message { Text(message).font(.footnote).foregroundStyle(.secondary) }
                Spacer()
            }
            .padding()
            .navigationTitle(event.date.formatted(date: .abbreviated, time: .shortened))
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
        }
        .presentationDetents([.large])
        .sensoryFeedback(.success, trigger: saved)
        .task { await load() }
        .onDisappear { player?.pause() }
    }

    private var subtitle: String {
        let time = event.date.formatted(date: .omitted, time: .shortened)
        return event.lengthText.map { "\(time) · \($0)" } ?? time
    }

    private func load() async {
        guard let hubEvent = event.event, let url = try? await client.clipURL(hubEvent) else { return }
        let p = AVPlayer(url: url)
        p.isMuted = true
        player = p
        p.play()
        file = try? await download(url)
    }

    /// The clip as a file named after the camera and time, for Share and Save.
    private func download(_ url: URL) async throws -> URL {
        if let file { return file }
        let (tmp, response) = try await URLSession.shared.download(from: url)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw HubError.unreachable }
        let stamp = event.date.formatted(.iso8601.year().month().day().time(includingFractionalSeconds: false))
            .replacingOccurrences(of: ":", with: ".")
        let dest = FileManager.default.temporaryDirectory.appendingPathComponent("\(event.camera.name) \(stamp).mp4")
        try? FileManager.default.removeItem(at: dest)
        try FileManager.default.moveItem(at: tmp, to: dest)
        return dest
    }

    private func saveToPhotos() async {
        guard let hubEvent = event.event else { return }
        working = true
        defer { working = false }
        do {
            var found = file
            if found == nil, let clip = try await client.clipURL(hubEvent) { found = try await download(clip) }
            guard let url = found else { throw HubError.unreachable }
            file = url
            let status = await PHPhotoLibrary.requestAuthorization(for: .addOnly)
            guard status == .authorized || status == .limited else {
                message = "Allow Lookout to add photos in Settings to save clips."
                return
            }
            try await PHPhotoLibrary.shared().performChanges {
                _ = PHAssetChangeRequest.creationRequestForAssetFromVideo(atFileURL: url)
            }
            message = "Saved to Photos."
            saved.toggle()
        } catch {
            message = "Couldn't save the clip: \(error.localizedDescription)"
        }
    }
}
