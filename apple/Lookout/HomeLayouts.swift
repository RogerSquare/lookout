import SwiftUI

/// How the home screen lays out the cameras (the phone held upright; sideways is always the wall).
enum HomeLayout: String, CaseIterable, Identifiable {
    case list, grid, focus

    var id: String { rawValue }

    var title: String {
        switch self {
        case .list: return "List"
        case .grid: return "Grid"
        case .focus: return "Focus"
        }
    }

    var symbol: String {
        switch self {
        case .list: return "rectangle.grid.1x2"
        case .grid: return "square.grid.2x2"
        case .focus: return "rectangle.topthird.inset.filled"
        }
    }
}

/// One camera's picture with its state on top: a person badge, an offline cover and (optionally)
/// its name on glass. Streams only while `live` and the camera is connected.
struct CameraTile: View {
    let camera: HubCamera
    let client: HubClient
    let live: Bool
    var frameWidth = 640
    var transport = LiveTransport.hls(variant: "phone")
    var compact = false
    var showsName = true
    var fill = false
    var cornerRadius: CGFloat = 14
    /// Extra room for the badges where the tile meets the screen's edge (rounded corners, cutout)
    var edgeInsets = EdgeInsets()

    @ObservedObject private var sleep = SleepState.shared
    /// An on-demand camera's newest picture from the hub (it isn't woken for this)
    @State private var thumbnail: UIImage?

    var body: some View {
        ZStack {
            if camera.isOnDemand {
                // Not streamed in a tile: that would keep a solar camera awake. Tap to open it.
                ZStack {
                    if let last = LiveModel.lastFrame(camera.id) ?? thumbnail {
                        Image(uiImage: last).resizable().aspectRatio(contentMode: fill ? .fill : .fit).opacity(0.7)
                    } else {
                        Color.black
                    }
                    VStack(spacing: compact ? 4 : 8) {
                        Image(systemName: "play.circle.fill").font(compact ? .title3 : .largeTitle)
                        if !compact {
                            Text("Tap to view").font(.subheadline.weight(.semibold))
                        }
                    }
                    .foregroundStyle(.white)
                }
                .clipped()
            } else if live && camera.connected && !camera.isAsleep {
                LiveCameraView(camera: camera, client: client, frameWidth: frameWidth, fill: fill, transport: transport)
            } else if (camera.isAsleep || sleep.isWaking(camera)), let last = LiveModel.lastFrame(camera.id) {
                // The last picture, dimmed, so it's clear which camera this is
                Color.black.overlay {
                    Image(uiImage: last).resizable().aspectRatio(contentMode: fill ? .fill : .fit)
                        .opacity(0.35).saturation(0.3)
                }
                .clipped()
            } else {
                Color.black
            }
            if camera.isAsleep && !sleep.isWaking(camera) {
                VStack(spacing: compact ? 4 : 8) {
                    Image(systemName: "moon.zzz.fill")
                        .font(compact ? .body : .title2)
                        .foregroundStyle(.indigo)
                    Text("Asleep")
                        .font(compact ? .caption.weight(.semibold) : .subheadline.weight(.semibold))
                    if !compact && camera.can("sleep") {
                        Button("Wake") { Task { await sleep.set(camera, asleep: false, client: client) } }
                            .buttonStyle(.glass)
                            .disabled(sleep.busy.contains(camera.id))
                    }
                }
            } else if sleep.isWaking(camera) {
                VStack(spacing: compact ? 4 : 8) {
                    ProgressView()
                    Text("Waking…")
                        .font(compact ? .caption.weight(.semibold) : .subheadline.weight(.semibold))
                }
                .foregroundStyle(.secondary)
            } else if !camera.connected {
                VStack(spacing: compact ? 4 : 8) {
                    Image(systemName: "video.slash.fill")
                        .font(compact ? .body : .title2)
                    Text("Offline")
                        .font(compact ? .caption.weight(.semibold) : .subheadline.weight(.semibold))
                }
                .foregroundStyle(.secondary)
            }
        }
        .overlay(alignment: .topLeading) {
            if camera.person {
                PersonBadge(compact: compact).padding(compact ? 6 : 10).padding(edgeInsets)
            }
        }
        .overlay(alignment: .bottomLeading) {
            if showsName {
                Text(camera.name)
                    .font(compact ? .caption.weight(.semibold) : .subheadline.weight(.semibold))
                    .lineLimit(1)
                    .padding(.horizontal, compact ? 8 : 10)
                    .padding(.vertical, compact ? 4 : 6)
                    .glassEffect(.regular, in: .capsule)
                    .padding(compact ? 6 : 10)
                    .padding(edgeInsets)
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: cornerRadius, style: .continuous))
        .contentShape(Rectangle())
        .task(id: camera.isOnDemand && live ? camera.id : "") {
            guard camera.isOnDemand, live else { return }
            while !Task.isCancelled {
                if let data = try? await client.frame(camera, width: frameWidth), let img = UIImage(data: data) {
                    thumbnail = img
                }
                try? await Task.sleep(nanoseconds: 60_000_000_000)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Self.label(camera))
        .accessibilityAddTraits(.isButton)
        .contextMenu {
            if camera.can("sleep") {
                Button {
                    Task { await sleep.set(camera, asleep: !camera.isAsleep, client: client) }
                } label: {
                    Label(camera.isAsleep ? "Wake" : "Sleep", systemImage: camera.isAsleep ? "sun.max" : "moon.zzz")
                }
            }
            if camera.can("ptz") && !camera.isAsleep {
                Section("Turn") {
                    Button { turn(-1, 0) } label: { Label("Left", systemImage: "arrow.left") }
                    Button { turn(1, 0) } label: { Label("Right", systemImage: "arrow.right") }
                    Button { turn(0, 1) } label: { Label("Up", systemImage: "arrow.up") }
                    Button { turn(0, -1) } label: { Label("Down", systemImage: "arrow.down") }
                }
            }
        }
    }

    private func turn(_ pan: Double, _ tilt: Double) {
        Task { try? await client.ptz(camera, pan: pan, tilt: tilt) }
    }

    static func label(_ camera: HubCamera) -> String {
        if camera.isAsleep { return "\(camera.name), asleep" }
        if !camera.connected { return "\(camera.name), offline" }
        if camera.person { return "\(camera.name), person now" }
        return camera.name
    }
}

/// "Person" on green, matching the watch's green edge and the status dot.
struct PersonBadge: View {
    var compact = false

    var body: some View {
        HStack(spacing: 4) {
            Image(systemName: "figure.stand")
            Text("Person")
        }
        .font(compact ? .caption2.weight(.bold) : .caption.weight(.bold))
        .foregroundStyle(.black)
        .padding(.horizontal, compact ? 6 : 8)
        .padding(.vertical, compact ? 3 : 4)
        .background(.green, in: Capsule())
    }
}

/// Two columns of smaller pictures. Tiles use the light MJPEG stream, and only the ones on
/// screen run (the grid is lazy).
struct CameraGrid: View {
    let cameras: [HubCamera]
    let client: HubClient
    let live: Bool
    let open: (HubCamera) -> Void

    private let columns = [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)]

    var body: some View {
        LazyVGrid(columns: columns, spacing: 12) {
            ForEach(cameras) { cam in
                Button { open(cam) } label: {
                    CameraTile(camera: cam, client: client, live: live, frameWidth: 480,
                               transport: .mjpeg(fps: 4), compact: true, fill: true)
                        .aspectRatio(4 / 3, contentMode: .fit)
                }
                .buttonStyle(.plain)
            }
        }
    }
}

/// One large picture with a strip of every camera under it; tap a small one to bring it up,
/// tap the large one for full screen. Starts on a camera with someone at it.
struct CameraFocus: View {
    let cameras: [HubCamera]
    let client: HubClient
    let live: Bool
    @Binding var focusedID: String?
    let open: (HubCamera) -> Void

    private var focused: HubCamera? {
        cameras.first { $0.id == focusedID } ?? cameras.first(where: \.person) ?? cameras.first
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if let cam = focused {
                Button { open(cam) } label: {
                    CameraTile(camera: cam, client: client, live: live, frameWidth: 1280, showsName: false, cornerRadius: 18)
                        .aspectRatio(16 / 9, contentMode: .fit)
                }
                .buttonStyle(.plain)
                .id(cam.id)        // a new camera starts its own stream rather than reusing the old one
                HStack {
                    Text(cam.name).font(.title3.weight(.semibold))
                    Spacer()
                    PersonStatus(camera: cam).foregroundStyle(.secondary)
                }
                .padding(.horizontal, 4)
            }
            if cameras.count > 1 {
                ScrollView(.horizontal, showsIndicators: false) {
                    LazyHStack(spacing: 12) {
                        ForEach(cameras) { cam in
                            let selected = cam.id == focused?.id
                            Button {
                                withAnimation(.snappy) { focusedID = cam.id }
                            } label: {
                                VStack(alignment: .leading, spacing: 6) {
                                    CameraTile(camera: cam, client: client, live: live, frameWidth: 320,
                                               transport: .mjpeg(fps: 2), compact: true, showsName: false,
                                               fill: true, cornerRadius: 12)
                                        .frame(width: 160, height: 90)
                                        .overlay {
                                            RoundedRectangle(cornerRadius: 12, style: .continuous)
                                                .strokeBorder(Color.accentColor, lineWidth: selected ? 3 : 0)
                                        }
                                    Text(cam.name)
                                        .font(.subheadline.weight(.semibold))
                                        .lineLimit(1)
                                        .foregroundStyle(selected ? .primary : .secondary)
                                }
                                .frame(width: 160)
                            }
                            .buttonStyle(.plain)
                            .accessibilityElement(children: .ignore)
                            .accessibilityLabel(CameraTile.label(cam))
                            .accessibilityAddTraits(selected ? [.isButton, .isSelected] : .isButton)
                        }
                    }
                    .padding(.vertical, 3)
                }
                .scrollClipDisabled()
            }
        }
    }
}

/// The phone turned sideways: every camera edge to edge, no bars. 1 fills the screen, 2 sit side
/// by side, 3-4 make a 2x2 wall, more go 3 across. Tap one for full screen.
struct CameraWall: View {
    let cameras: [HubCamera]
    let client: HubClient
    let live: Bool
    let open: (HubCamera) -> Void

    private let gap: CGFloat = 2
    /// Clearance from the screen's rounded corners for tiles on the edge
    private let edge: CGFloat = 14

    var body: some View {
        GeometryReader { geo in
            // Laid out over the whole screen, under the camera cutout and rounded corners; tiles
            // on an edge keep their badges clear of them
            let safe = geo.safeAreaInsets
            let size = CGSize(width: geo.size.width + safe.leading + safe.trailing,
                              height: geo.size.height + safe.top + safe.bottom)
            let n = cameras.count
            let cols = n <= 1 ? 1 : (n <= 4 ? 2 : 3)
            let rows = max(1, Int(ceil(Double(n) / Double(cols))))
            let height = (size.height - CGFloat(rows - 1) * gap) / CGFloat(rows)
            let few = n <= 2
            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: gap), count: cols), spacing: gap) {
                ForEach(Array(cameras.enumerated()), id: \.element.id) { i, cam in
                    let col = i % cols, row = i / cols
                    Button { open(cam) } label: {
                        CameraTile(camera: cam, client: client, live: live, frameWidth: few ? 1280 : 640,
                                   transport: few ? .hls(variant: "phone") : .mjpeg(fps: 5),
                                   compact: !few, fill: true, cornerRadius: 0,
                                   edgeInsets: EdgeInsets(top: row == 0 ? max(safe.top, edge) : 0,
                                                          leading: col == 0 ? max(safe.leading, edge) : 0,
                                                          bottom: row == rows - 1 ? max(safe.bottom, edge) : 0,
                                                          trailing: col == cols - 1 ? max(safe.trailing, edge) : 0))
                            .frame(height: height)
                    }
                    .buttonStyle(.plain)
                }
            }
            .frame(width: size.width, height: size.height)
            .offset(x: -safe.leading, y: -safe.top)
        }
        .background(.black)
    }
}

/// Order and show/hide for the iPhone's home screen. The order is shared with the watch;
/// hidden cameras only leave the iPhone's home screen (they still alert).
struct ArrangeCamerasView: View {
    @ObservedObject var store: CameraStore
    @ObservedObject private var prefs = Prefs.shared
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                Section {
                    ForEach(prefs.sorted(store.cameras)) { cam in
                        let shown = prefs.showsOnPhone(cam.id)
                        Button {
                            prefs.setShowsOnPhone(cam.id, !shown)
                        } label: {
                            HStack(spacing: 12) {
                                Image(systemName: shown ? "checkmark.circle.fill" : "circle")
                                    .font(.title3)
                                    .foregroundStyle(shown ? Color.accentColor : Color.secondary)
                                Text(cam.name).foregroundStyle(.primary)
                                Spacer()
                                PersonStatus(camera: cam).foregroundStyle(.secondary)
                            }
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel(cam.name)
                        .accessibilityValue(shown ? "Shown" : "Hidden")
                    }
                    .onMove { prefs.move(store.cameras, from: $0, to: $1) }
                } footer: {
                    Text("Drag to change the order on your iPhone and Apple Watch. Hidden cameras still send alerts.")
                }
            }
            .environment(\.editMode, .constant(.active))
            .navigationTitle("Arrange Cameras")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }
}
