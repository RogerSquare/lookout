import AVFoundation
import AVKit
import Photos
import SwiftUI

/// A camera full screen: swipe sideways between cameras, pinch (or double-tap) to zoom, save a
/// snapshot, sound, Picture in Picture and the camera's own controls. Upright, the picture sits on
/// top with the controls under it; sideways it fills the screen and the controls fade out.
struct CameraPlayer: View {
    /// The cameras to swipe through, in the user's order (kept fresh by the caller)
    let cameras: [HubCamera]
    let client: HubClient
    let close: () -> Void

    @StateObject private var model = LiveModel()
    @StateObject private var pip = PiPController()
    @State private var currentID: String?
    @State private var shownID: String?
    /// Full-resolution stream: on by default at home, the light one away (data and the tunnel)
    @State private var hd = HubRoute.isHome
    @State private var zoom: CGFloat = 1
    @State private var zoomBase: CGFloat = 1
    @State private var offset: CGSize = .zero
    @State private var offsetBase: CGSize = .zero
    @State private var controlsShown = true
    @State private var hideTask: Task<Void, Never>?
    @State private var flash = false
    @State private var photosDenied = false
    @State private var saved = false
    /// Move mode (cameras that can turn): a drag on the picture turns the camera instead of
    /// swiping to the next one
    @State private var moveMode = false
    @State private var turnDrag: CGSize = .zero
    @State private var turning = false
    @State private var turnError: String?

    init(cameras: [HubCamera], client: HubClient, startID: String, close: @escaping () -> Void) {
        self.cameras = cameras
        self.client = client
        self.close = close
        _currentID = State(initialValue: startID)
    }

    private var current: HubCamera? { cameras.first { $0.id == currentID } ?? cameras.first }

    private var variant: String {
        guard let cam = current else { return "phone" }
        // An on-demand camera's own stream is small already: converting it only adds a second
        if cam.isOnDemand && cam.offers("hd") { return "hd" }
        return hd && cam.offers("hd") ? "hd" : "phone"
    }

    var body: some View {
        GeometryReader { geo in
            if geo.size.width > geo.size.height {
                landscape
            } else {
                portrait(width: geo.size.width)
            }
        }
        #if DEBUG
        .modifier(FakeLandscape())
        #endif
        .background(Color.black.ignoresSafeArea())
        .preferredColorScheme(.dark)
        .task(id: "\(current?.id ?? "")|\(variant)") {
            guard let cam = current else { return }
            if shownID != cam.id {
                model.reset()          // another camera: don't show the last one's picture
                shownID = cam.id
            }
            model.capturesFrames = true
            await model.run(cam, client: client, width: 1280, transport: .hls(variant: variant))
        }
        .onAppear {
            PlayerAudio.start(muted: model.muted)
            scheduleHide()
        }
        .onDisappear {
            pip.stop()
            PlayerAudio.end()
        }
        .onChange(of: model.muted) { _, muted in PlayerAudio.start(muted: muted) }
        .onChange(of: currentID) { _, _ in
            resetZoom(animated: false)
            if current?.can("ptz") != true { moveMode = false }
        }
        .alert("Couldn't turn the camera", isPresented: Binding(get: { turnError != nil }, set: { if !$0 { turnError = nil } })) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(turnError ?? "")
        }
        .alert("Lookout can't add photos", isPresented: $photosDenied) {
            Button("Open Settings") {
                if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Allow Lookout to add photos in Settings to save snapshots.")
        }
        .sensoryFeedback(.success, trigger: saved)
    }

    // MARK: Layouts

    /// Upright: the name on top, the 16:9 picture, then the buttons and the camera's controls.
    private func portrait(width: CGFloat) -> some View {
        VStack(spacing: 0) {
            topBar
                .padding(.horizontal)
                .padding(.vertical, 8)
            pager
                .frame(width: width, height: width * 9 / 16)
            pageDots
                .padding(.top, 10)
            ScrollView {
                VStack(spacing: 24) {
                    actionButtons
                    if let cam = current {
                        CameraControlsBar(camera: cam, client: client)
                    }
                }
                .padding(.top, 20)
                .padding(.horizontal)
                .frame(maxWidth: .infinity)
            }
            .scrollBounceBehavior(.basedOnSize)
        }
        .statusBarHidden(false)
    }

    /// Sideways: the picture edge to edge; tap it for the controls, which fade after a few seconds.
    private var landscape: some View {
        // The picture ignores the safe area; the controls keep to it (clear of the Dynamic Island)
        ZStack {
            pager
                .ignoresSafeArea()
            if controlsShown {
                VStack {
                    topBar
                    Spacer()
                    HStack(alignment: .bottom) {
                        actionButtons
                        Spacer()
                        if let cam = current {
                            CameraControlsBar(camera: cam, client: client)
                        }
                    }
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 12)
                .transition(.opacity)
            }
        }
            .statusBarHidden(true)
            .persistentSystemOverlays(controlsShown ? .automatic : .hidden)
            .animation(.easeOut(duration: 0.25), value: controlsShown)
    }

    // MARK: Pager

    /// One page per camera. Only the page on screen streams; the others show a still, so a swipe
    /// lands on a picture straight away.
    private var pager: some View {
        ScrollView(.horizontal) {
            LazyHStack(spacing: 0) {
                ForEach(cameras) { cam in
                    Group {
                        if cam.id == current?.id {
                            stage(cam)
                        } else {
                            StillFrame(camera: cam, client: client)
                        }
                    }
                    .containerRelativeFrame([.horizontal, .vertical])
                    .clipped()
                    .id(cam.id)
                }
            }
            .scrollTargetLayout()
        }
        .scrollTargetBehavior(.paging)
        .scrollPosition(id: $currentID)
        .scrollIndicators(.hidden)
        .scrollDisabled(zoom > 1 || cameras.count < 2 || moveMode)
        .background(.black)
    }

    private func stage(_ cam: HubCamera) -> some View {
        GeometryReader { geo in
            ZStack {
                PlayerSurface(model: model, pip: pip)
                    .scaleEffect(zoom)
                    .offset(offset)
                if !cam.connected {
                    VStack(spacing: 8) {
                        Image(systemName: "video.slash.fill").font(.title2)
                        Text("Offline").font(.subheadline.weight(.semibold))
                    }
                    .foregroundStyle(.secondary)
                }
                Color.white
                    .opacity(flash ? 0.7 : 0)
                    .allowsHitTesting(false)
            }
            .overlay { if moveMode { turnGuide(geo.size) } }
            .contentShape(Rectangle())
            .gesture(magnify(geo.size))
            .gesture(drag(geo.size), including: zoom > 1 ? .all : .subviews)
            .gesture(turn(cam, geo.size), including: moveMode && zoom == 1 ? .all : .subviews)
            .onTapGesture(count: 2) { point in if !moveMode { doubleTap(at: point, in: geo.size) } }
            .onTapGesture { toggleControls() }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("\(cam.name), live")
            .accessibilityAddTraits(.isImage)
            .accessibilityZoomAction { action in
                withAnimation { zoom = action.direction == .zoomIn ? min(zoom + 1, 5) : max(zoom - 1, 1) }
                if zoom == 1 { offset = .zero }
                zoomBase = zoom
                offsetBase = offset
            }
        }
    }

    // MARK: Bars

    private var topBar: some View {
        HStack(spacing: 12) {
            Button(action: close) {
                Image(systemName: "xmark")
                    .font(.body.weight(.semibold))
                    .frame(width: 44, height: 44)
            }
            .buttonStyle(.plain)
            .glassEffect(.regular.interactive(), in: .circle)
            .accessibilityLabel("Close")
            if let cam = current {
                VStack(alignment: .leading, spacing: 2) {
                    Text(cam.name).font(.headline).lineLimit(1)
                    PersonStatus(camera: cam).foregroundStyle(.secondary)
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 6)
                .glassEffect(.regular, in: .capsule)
                .accessibilityElement(children: .combine)
            }
            Spacer()
        }
    }

    @ViewBuilder private var pageDots: some View {
        if cameras.count > 1 {
            HStack(spacing: 7) {
                ForEach(cameras) { cam in
                    Circle()
                        .fill(cam.id == current?.id ? Color.primary : Color.secondary.opacity(0.4))
                        .frame(width: 7, height: 7)
                }
            }
            .accessibilityHidden(true)
        }
    }

    private var actionButtons: some View {
        HStack(spacing: 12) {
            RoundButton(symbol: "camera", label: "Save Snapshot") { snapshot() }
                .disabled(model.still == nil && !model.video)
            RoundButton(symbol: model.muted ? "speaker.slash.fill" : "speaker.wave.2.fill",
                        label: model.muted ? "Turn Sound On" : "Turn Sound Off") {
                model.muted.toggle()
                scheduleHide()
            }
            .disabled(!model.hasAudio)
            if AVPictureInPictureController.isPictureInPictureSupported() {
                RoundButton(symbol: pip.active ? "pip.exit" : "pip.enter", label: "Picture in Picture") {
                    pip.toggle()
                }
                .disabled(!pip.possible)
            }
            if current?.can("ptz") == true {
                Button {
                    moveMode.toggle()
                    if moveMode { resetZoom(animated: true) }
                    scheduleHide()
                } label: {
                    Image(systemName: "arrow.up.and.down.and.arrow.left.and.right")
                        .font(.system(size: 18, weight: .semibold))
                        .foregroundStyle(moveMode ? Color.black : Color.primary)
                        .frame(width: 50, height: 50)
                        .background(Circle().fill(moveMode ? Color.white : Color.clear))
                }
                .buttonStyle(.plain)
                .glassEffect(.regular.interactive(), in: .circle)
                .accessibilityLabel("Move Camera")
                .accessibilityValue(moveMode ? "On: drag the picture to turn the camera" : "Off")
            }
            if current?.offers("hd") == true {
                Button {
                    hd.toggle()
                    scheduleHide()
                } label: {
                    Text("HD")
                        .font(.footnote.weight(.bold))
                        .foregroundStyle(hd ? Color.black : Color.primary)
                        .frame(width: 50, height: 50)
                        .background(Circle().fill(hd ? Color.white : Color.clear))
                }
                .buttonStyle(.plain)
                .glassEffect(.regular.interactive(), in: .circle)
                .accessibilityLabel("High Quality")
                .accessibilityValue(hd ? "On" : "Off")
            }
        }
    }

    // MARK: Gestures

    private func magnify(_ size: CGSize) -> some Gesture {
        MagnifyGesture()
            .onChanged { value in
                zoom = min(max(zoomBase * value.magnification, 1), 5)
                offset = clamped(offset, size)
            }
            .onEnded { _ in
                if zoom < 1.05 { resetZoom(animated: true) }
                zoomBase = zoom
                offsetBase = offset
            }
    }

    private func drag(_ size: CGSize) -> some Gesture {
        DragGesture()
            .onChanged { value in
                offset = clamped(CGSize(width: offsetBase.width + value.translation.width,
                                        height: offsetBase.height + value.translation.height), size)
            }
            .onEnded { _ in offsetBase = offset }
    }

    /// Move mode: drag the picture the way you want to look. Dragging across half the picture is
    /// a full step (about 30 degrees); the camera turns once the finger lifts.
    private func turn(_ cam: HubCamera, _ size: CGSize) -> some Gesture {
        DragGesture(minimumDistance: 8)
            .onChanged { value in turnDrag = value.translation }
            .onEnded { value in
                turnDrag = .zero
                // Dragging the picture right shows what's to the left: the camera turns left
                let pan = max(-1, min(1, -value.translation.width / max(1, size.width / 2)))
                let tilt = max(-1, min(1, value.translation.height / max(1, size.height / 2)))
                guard abs(pan) > 0.08 || abs(tilt) > 0.08, !turning else { return }
                turning = true
                UIImpactFeedbackGenerator(style: .light).impactOccurred()
                Task {
                    defer { turning = false }
                    do {
                        try await client.ptz(cam, pan: abs(pan) > 0.08 ? pan : 0, tilt: abs(tilt) > 0.08 ? tilt : 0)
                    } catch HubError.status(409) {
                        // still moving from the last drag
                    } catch {
                        turnError = error.localizedDescription
                        UINotificationFeedbackGenerator().notificationOccurred(.error)
                    }
                }
            }
    }

    /// Where the camera will turn, while dragging; a spinner while it's moving.
    private func turnGuide(_ size: CGSize) -> some View {
        ZStack {
            if turning {
                ProgressView().controlSize(.large).tint(.white)
            } else if turnDrag != .zero {
                Image(systemName: "arrow.up")
                    .font(.system(size: 34, weight: .bold))
                    .foregroundStyle(.white)
                    .padding(18)
                    .glassEffect(.regular, in: .circle)
                    // points the way the camera will look: against the drag
                    .rotationEffect(.radians(atan2(-turnDrag.width, turnDrag.height)))
            } else {
                RoundedRectangle(cornerRadius: 0)
                    .strokeBorder(Color.white.opacity(0.6), lineWidth: 3)
            }
        }
        .allowsHitTesting(false)
    }

    /// 1x <-> 2x, zooming in on the spot that was tapped.
    private func doubleTap(at point: CGPoint, in size: CGSize) {
        withAnimation(.snappy) {
            if zoom > 1 {
                zoom = 1
                offset = .zero
            } else {
                zoom = 2
                offset = clamped(CGSize(width: (size.width / 2 - point.x) * 2,
                                        height: (size.height / 2 - point.y) * 2), size)
            }
        }
        zoomBase = zoom
        offsetBase = offset
    }

    /// Keep the zoomed picture covering the view: never slide past its edges.
    private func clamped(_ o: CGSize, _ size: CGSize) -> CGSize {
        let maxX = size.width * (zoom - 1) / 2
        let maxY = size.height * (zoom - 1) / 2
        return CGSize(width: min(max(o.width, -maxX), maxX), height: min(max(o.height, -maxY), maxY))
    }

    private func resetZoom(animated: Bool) {
        let reset = {
            zoom = 1
            offset = .zero
        }
        if animated { withAnimation(.snappy, reset) } else { reset() }
        zoomBase = 1
        offsetBase = .zero
    }

    // MARK: Controls fade (sideways only; upright they stay)

    private func toggleControls() {
        controlsShown.toggle()
        scheduleHide()
    }

    private func scheduleHide() {
        hideTask?.cancel()
        guard controlsShown else { return }
        hideTask = Task {
            try? await Task.sleep(nanoseconds: 4_000_000_000)
            if !Task.isCancelled { controlsShown = false }
        }
    }

    // MARK: Snapshot

    private func snapshot() {
        guard let image = model.snapshot() else { return }
        withAnimation(.easeOut(duration: 0.08)) { flash = true }
        withAnimation(.easeIn(duration: 0.3).delay(0.08)) { flash = false }
        scheduleHide()
        Task {
            switch await SnapshotSaver.save(image) {
            case .saved: saved.toggle()
            case .denied: photosDenied = true
            case .failed: break
            }
        }
    }
}

/// Saves a picture to Photos with add-only access (asked for the first time).
enum SnapshotSaver {
    enum Outcome { case saved, denied, failed }

    @MainActor static func save(_ image: UIImage) async -> Outcome {
        let status = await PHPhotoLibrary.requestAuthorization(for: .addOnly)
        guard status == .authorized || status == .limited else { return .denied }
        do {
            try await PHPhotoLibrary.shared().performChanges {
                PHAssetChangeRequest.creationRequestForAsset(from: image)
            }
            return .saved
        } catch {
            return .failed
        }
    }
}

/// A glass circle with a symbol, the player's button style.
struct RoundButton: View {
    let symbol: String
    let label: String
    let action: () -> Void
    @Environment(\.isEnabled) private var enabled

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 18, weight: .semibold))
                .frame(width: 50, height: 50)
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .opacity(enabled ? 1 : 0.35)
        .glassEffect(.regular.interactive(), in: .circle)
        .accessibilityLabel(label)
    }
}

/// The picture: the still at once, the video on top once it's moving. Drawn by an AVPlayerLayer
/// (not SwiftUI's VideoPlayer) so it can go Picture in Picture.
struct PlayerSurface: View {
    @ObservedObject var model: LiveModel
    var pip: PiPController?
    var fill = false

    var body: some View {
        Color.black
            .overlay {
                if let still = model.still {
                    Image(uiImage: still).resizable().aspectRatio(contentMode: fill ? .fill : .fit)
                }
            }
            .overlay {
                PlayerLayerView(player: model.player, pip: pip, fill: fill)
                    .opacity(model.video ? 1 : 0)
                    .allowsHitTesting(false)
            }
            .overlay {
                if model.still == nil && !model.video { ProgressView() }
            }
            .clipped()
    }
}

/// A neighbouring camera's page: one still frame, no stream.
private struct StillFrame: View {
    let camera: HubCamera
    let client: HubClient
    @State private var image: UIImage?

    var body: some View {
        Color.black
            .overlay {
                if let image { Image(uiImage: image).resizable().aspectRatio(contentMode: .fit) }
            }
            .task(id: camera.id) {
                #if DEBUG
                if Demo.isDemo(camera) {
                    image = Demo.image(camera)
                    return
                }
                #endif
                guard camera.connected else { return }
                if let data = try? await client.frame(camera, width: 640) { image = UIImage(data: data) }
            }
    }
}

struct PlayerLayerView: UIViewRepresentable {
    let player: AVPlayer?
    var pip: PiPController?
    var fill = false

    final class LayerView: UIView {
        override static var layerClass: AnyClass { AVPlayerLayer.self }
        var playerLayer: AVPlayerLayer { layer as! AVPlayerLayer }
    }

    func makeUIView(context: Context) -> LayerView {
        let view = LayerView()
        view.backgroundColor = .clear
        view.playerLayer.videoGravity = fill ? .resizeAspectFill : .resizeAspect
        view.playerLayer.player = player
        pip?.attach(view.playerLayer)
        return view
    }

    func updateUIView(_ view: LayerView, context: Context) {
        if view.playerLayer.player !== player { view.playerLayer.player = player }
        view.playerLayer.videoGravity = fill ? .resizeAspectFill : .resizeAspect
    }
}

/// Picture in Picture for the player's layer. Starts by itself when the app goes to the
/// background with a camera playing.
@MainActor
final class PiPController: NSObject, ObservableObject {
    @Published private(set) var possible = false
    @Published private(set) var active = false
    private var controller: AVPictureInPictureController?
    private var observation: NSKeyValueObservation?

    func attach(_ layer: AVPlayerLayer) {
        guard AVPictureInPictureController.isPictureInPictureSupported(),
              controller?.contentSource?.playerLayer !== layer else { return }
        let c = AVPictureInPictureController(contentSource: .init(playerLayer: layer))
        c.delegate = self
        c.canStartPictureInPictureAutomaticallyFromInline = true
        observation = c.observe(\.isPictureInPicturePossible, options: [.initial, .new]) { [weak self] c, _ in
            let possible = c.isPictureInPicturePossible
            Task { @MainActor in self?.possible = possible }
        }
        controller = c
    }

    func toggle() {
        guard let c = controller else { return }
        if c.isPictureInPictureActive { c.stopPictureInPicture() } else { c.startPictureInPicture() }
    }

    func stop() {
        if controller?.isPictureInPictureActive == true { controller?.stopPictureInPicture() }
    }
}

extension PiPController: AVPictureInPictureControllerDelegate {
    nonisolated func pictureInPictureControllerDidStartPictureInPicture(_ c: AVPictureInPictureController) {
        Task { @MainActor in self.active = true }
    }

    nonisolated func pictureInPictureControllerDidStopPictureInPicture(_ c: AVPictureInPictureController) {
        Task { @MainActor in self.active = false }
    }

    /// The player is still on screen under the floating window, so there's nothing to rebuild.
    nonisolated func pictureInPictureController(_ c: AVPictureInPictureController,
                                                restoreUserInterfaceForPictureInPictureStopWithCompletionHandler
                                                completion: @escaping (Bool) -> Void) {
        completion(true)
    }
}

/// The app's audio while the player is open: "playback" (what Picture in Picture needs), mixed
/// with other apps' sound while muted so opening a camera doesn't stop the user's music.
enum PlayerAudio {
    static func start(muted: Bool) {
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playback, mode: .moviePlayback, options: muted ? [.mixWithOthers] : [])
        try? session.setActive(true)
    }

    /// Back to the home screen's silent tiles, which never interrupt other audio.
    static func end() {
        let session = AVAudioSession.sharedInstance()
        try? session.setActive(false, options: .notifyOthersOnDeactivation)
        try? session.setCategory(.ambient)
    }
}

/// What a camera can be told to do, from the hub's `caps` (and the older `ptz` flag).
enum CameraControl: Hashable {
    case panTilt, presets, sleep, nightVision

    static func available(_ cam: HubCamera) -> [CameraControl] {
        let caps = Set(cam.caps ?? [])
        var list: [CameraControl] = []
        if cam.ptz == true || caps.contains("ptz") { list.append(.panTilt) }
        if caps.contains("presets") { list.append(.presets) }
        if caps.contains("sleep") { list.append(.sleep) }
        if caps.contains("night_vision") { list.append(.nightVision) }
        return list
    }
}

/// The camera's own controls under (or beside) the picture: the pan/tilt pad, sleep and night
/// vision, each shown only when the hub says the camera has it.
struct CameraControlsBar: View {
    let camera: HubCamera
    let client: HubClient
    @State private var error: String?

    var body: some View {
        let controls = CameraControl.available(camera)
        if !controls.isEmpty {
            HStack(alignment: .center, spacing: 16) {
                if controls.contains(.sleep) || controls.contains(.nightVision) {
                    VStack(spacing: 12) {
                        if controls.contains(.sleep) {
                            RoundButton(symbol: camera.isAsleep ? "sun.max.fill" : "moon.zzz.fill",
                                        label: camera.isAsleep ? "Wake Camera" : "Put Camera to Sleep") {
                                Task { await SleepState.shared.set(camera, asleep: !camera.isAsleep, client: client) }
                            }
                        }
                        if controls.contains(.nightVision) {
                            Menu {
                                Button("Automatic") { send("night_vision", "auto") }
                                Button("On") { send("night_vision", "on") }
                                Button("Off") { send("night_vision", "off") }
                            } label: {
                                Image(systemName: "moon.stars.fill")
                                    .font(.system(size: 18, weight: .semibold))
                                    .foregroundStyle(Color.primary)
                                    .frame(width: 50, height: 50)
                            }
                            .glassEffect(.regular.interactive(), in: .circle)
                            .accessibilityLabel("Night Vision")
                        }
                    }
                }
                if controls.contains(.panTilt) {
                    VStack(spacing: 12) {
                        PanTiltPad(camera: camera, client: client)
                        if controls.contains(.presets) { PresetChips(camera: camera, client: client) }
                    }
                }
            }
            .alert("Couldn't change the camera", isPresented: Binding(get: { error != nil }, set: { if !$0 { error = nil } })) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(error ?? "")
            }
        }
    }

    private func send(_ action: String, _ value: String?) {
        Task {
            do {
                try await client.control(camera, action: action, value: value)
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}

#if DEBUG
/// -LookoutFakeLandscape: the player drawn sideways on an upright screen (the simulator can't be
/// rotated over SSH); rotate the screenshot 90 degrees to check it.
private struct FakeLandscape: ViewModifier {
    private let on = ProcessInfo.processInfo.arguments.contains("-LookoutFakeLandscape")

    func body(content: Content) -> some View {
        if on {
            GeometryReader { geo in
                let full = CGSize(width: geo.size.width + geo.safeAreaInsets.leading + geo.safeAreaInsets.trailing,
                                  height: geo.size.height + geo.safeAreaInsets.top + geo.safeAreaInsets.bottom)
                content
                    .frame(width: full.height, height: full.width)
                    .rotationEffect(.degrees(90))
                    .frame(width: full.width, height: full.height)
            }
            .ignoresSafeArea()
        } else {
            content
        }
    }
}
#endif

/// The camera's saved positions as chips (1, 2, 3...): tap one to turn there. Positions are
/// saved in the Wyze app; the bridge can't save them yet.
struct PresetChips: View {
    let camera: HubCamera
    let client: HubClient
    @State private var presets: [PTZPreset] = []
    @State private var busy: Int?
    @State private var failed = false

    var body: some View {
        HStack(spacing: 8) {
            ForEach(presets.indices, id: \.self) { i in
                Button {
                    Task { await go(i + 1) }
                } label: {
                    ZStack {
                        if busy == i + 1 { ProgressView().controlSize(.small) }
                        else { Text("\(i + 1)").font(.subheadline.weight(.semibold)) }
                    }
                    .frame(width: 40, height: 36)
                }
                .buttonStyle(.plain)
                .glassEffect(.regular.interactive(), in: .capsule)
                .accessibilityLabel("Saved position \(i + 1)")
            }
        }
        .sensoryFeedback(.error, trigger: failed)
        .task(id: camera.id) {
            presets = (try? await client.ptzPresets(camera)) ?? []
        }
    }

    private func go(_ n: Int) async {
        guard busy == nil else { return }
        busy = n
        defer { busy = nil }
        do { try await client.ptz(camera, preset: n) } catch HubError.status(409) {} catch { failed.toggle() }
    }
}
