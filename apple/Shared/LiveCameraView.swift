import AVFoundation
import AVKit
#if os(iOS)
import CoreImage
#endif
import SwiftUI

/// How a live view gets its picture.
enum LiveTransport: Equatable {
    /// The hub's multipart JPEG stream, drawn as images: no video player, no controls, ~0.3 s
    /// behind. Used on the watch.
    case mjpeg(fps: Int)
    /// HLS through AVPlayer (the hub's go2rtc stream, `variant` e.g. "phone"). Used on the iPhone.
    case hls(variant: String?)
}

/// Runs one camera. Always starts with the hub's still frame so there's a picture at once.
/// HLS: switches to the video once its playhead is actually moving, covers stalls with stills
/// and restarts a dead stream. MJPEG: every frame replaces the still; reconnects on drop.
@MainActor
final class LiveModel: ObservableObject {
    @Published var still: UIImage? {
        didSet { if let still, let cameraID { Self.lastFrames[cameraID] = still } }
    }
    /// The newest picture of each camera, from any view showing it: a camera that appears
    /// somewhere else (a Duo folding, the full-screen player opening) starts with it instead of a
    /// spinner. Small: one image per camera.
    private static var lastFrames: [String: UIImage] = [:]
    /// The last picture any view had of this camera (shown dimmed while it sleeps)
    static func lastFrame(_ cameraID: String) -> UIImage? { lastFrames[cameraID] }
    private var cameraID: String?
    @Published var player: AVPlayer?
    @Published var playing = false
    @Published var video = false   // the picture is actually moving: show the player, not the still
    /// The stream has a sound track (only known once the video is playing)
    @Published var hasAudio = false
    /// Sound off by default; applies to the current player and any restarted one
    @Published var muted = true {
        didSet { player?.isMuted = muted }
    }
    /// Keep a video output on the player so `snapshot()` can grab a full-size frame (iPhone
    /// full-screen player only: it costs a copy per frame)
    var capturesFrames = false
    private var observer: NSKeyValueObservation?
    #if os(iOS)
    private var output: AVPlayerItemVideoOutput?
    #endif

    /// Forget the last camera's picture before showing another one
    func reset() {
        stop()
        still = nil
        hasAudio = false
    }

    #if os(iOS)
    /// The picture on screen right now: the video's current frame at full size when it's
    /// playing, otherwise the latest still.
    func snapshot() -> UIImage? {
        if video, let output, let item = player?.currentItem {
            let time = output.itemTime(forHostTime: CACurrentMediaTime())
            let at = time.isValid ? time : item.currentTime()
            if let buffer = output.copyPixelBuffer(forItemTime: at, itemTimeForDisplay: nil) {
                let image = CIImage(cvPixelBuffer: buffer)
                if let cg = CIContext().createCGImage(image, from: image.extent) {
                    return UIImage(cgImage: cg)
                }
            }
        }
        return still
    }
    #endif

    func run(_ cam: HubCamera, client: HubClient, width: Int, transport: LiveTransport) async {
        cameraID = cam.id
        if still == nil, let last = Self.lastFrames[cam.id] { still = last }
        #if DEBUG && os(iOS)
        if Demo.isDemo(cam) {
            still = Demo.image(cam)
            return
        }
        #endif
        switch transport {
        case .mjpeg(let fps): await runMJPEG(cam, client: client, width: width, fps: fps)
        case .hls(let variant): await runHLS(cam, client: client, width: width, variant: variant)
        }
    }

    private func runMJPEG(_ cam: HubCamera, client: HubClient, width: Int, fps: Int) async {
        if let data = try? await client.frame(cam, width: width), let img = UIImage(data: data) {
            still = img
        }
        while !Task.isCancelled {
            var ended = false
            let stream = MJPEGStream(onFrame: { [weak self] data in
                guard let img = UIImage(data: data) else { return }
                Task { @MainActor in self?.still = img }
            }, onEnd: { _ in
                Task { @MainActor in ended = true }
            })
            guard let url = await client.mjpegURL(cam, width: width, fps: fps) else {
                try? await Task.sleep(nanoseconds: 2_000_000_000)    // hub unreachable: try again
                continue
            }
            stream.start(url)
            while !Task.isCancelled && !ended {
                try? await Task.sleep(nanoseconds: 250_000_000)
            }
            stream.stop()
            if !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 1_000_000_000)   // dropped: reconnect shortly
            }
        }
    }

    private func runHLS(_ cam: HubCamera, client: HubClient, width: Int, variant: String?) async {
        var lastPosition = -1.0
        var lastProgress = Date()
        // Picture first, video second: the still is on screen even if video never starts
        if let data = try? await client.frame(cam, width: width), let img = UIImage(data: data) {
            still = img
        }
        guard VideoGuard.allowed else {
            // Video crashed this app last time: near-live stills instead (~3 per second)
            while !Task.isCancelled {
                if let data = try? await client.frame(cam, width: width), let img = UIImage(data: data) {
                    still = img
                }
                try? await Task.sleep(nanoseconds: 300_000_000)
            }
            return
        }
        await startVideo(cam, client, variant)
        while !Task.isCancelled {
            // "Playing" isn't enough: a stalled live stream can report playing with a frozen
            // picture. Watch the playhead instead, and start over if it stops moving.
            let position = player?.currentTime().seconds ?? -1
            if position.isFinite, position > lastPosition + 0.1 {
                lastPosition = position
                lastProgress = Date()
            }
            // watchOS's VideoPlayer resets the rate to 0 when it attaches; keep asking to play
            if let p = player, p.rate == 0 { p.play() }
            let moving = playing && Date().timeIntervalSince(lastProgress) < 1.2
            if video != moving { video = moving }
            let audio = player?.currentItem?.tracks.contains { $0.assetTrack?.mediaType == .audio } ?? false
            if audio && !hasAudio { hasAudio = true }
            if moving { VideoGuard.started() }
            if !moving, let data = try? await client.frame(cam, width: width), let img = UIImage(data: data) {
                still = img
            }
            // Dead stream (the go2rtc session expired, or the item failed): start over quickly.
            // An on-demand camera is woken first (several seconds), so it gets longer to start.
            let failed = player?.currentItem?.status == .failed
            let patience: TimeInterval = cam.isOnDemand && lastPosition < 0 ? 30 : 5
            if player != nil, failed || Date().timeIntervalSince(lastProgress) > patience {
                await startVideo(cam, client, variant)
                lastPosition = -1
                lastProgress = Date()
            }
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
        stop()
    }

    private func startVideo(_ cam: HubCamera, _ client: HubClient, _ variant: String?) async {
        stop()
        guard let url = await client.hlsURL(cam, variant: variant) else { return }
        VideoGuard.starting()
        let item = AVPlayerItem(url: url)
        item.preferredForwardBufferDuration = 1
        #if os(iOS)
        if capturesFrames {
            let out = AVPlayerItemVideoOutput(pixelBufferAttributes: [
                kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
            item.add(out)
            output = out
        }
        #endif
        let p = AVPlayer(playerItem: item)
        p.isMuted = muted
        p.automaticallyWaitsToMinimizeStalling = false
        observer = p.observe(\.timeControlStatus, options: [.new]) { [weak self] p, _ in
            let isPlaying = p.timeControlStatus == .playing
            Task { @MainActor in self?.playing = isPlaying }
        }
        player = p
        p.play()
    }

    func stop() {
        player?.pause()
        player = nil
        observer = nil
        #if os(iOS)
        output = nil
        #endif
        playing = false
        video = false
    }
}

/// Remembers whether starting video killed the app. A flag is set before the player starts and
/// cleared once frames are moving; finding it still set at the next launch means the app died in
/// between, so that device sticks to still frames for this build.
enum VideoGuard {
    private static let defaults = UserDefaults.standard
    private static let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "0"
    private static let pendingKey = "videoStartPending"
    private static let disabledKey = "videoDisabledForBuild"

    /// Call once at launch.
    static func checkLastRun() {
        if defaults.bool(forKey: pendingKey) {
            defaults.set(build, forKey: disabledKey)
        }
        defaults.set(false, forKey: pendingKey)
    }

    static var allowed: Bool { defaults.string(forKey: disabledKey) != build }
    static func starting() { defaults.set(true, forKey: pendingKey) }
    static func started() { if defaults.bool(forKey: pendingKey) { defaults.set(false, forKey: pendingKey) } }
    /// Leaving the app normally (screen down, app switched) isn't a crash.
    static func leftNormally() { started() }
}

struct LiveCameraView: View {
    let camera: HubCamera
    let client: HubClient
    var frameWidth = 400
    var fill = false
    var transport = LiveTransport.hls(variant: nil)
    /// Horizontal pan while filled, -1 (left edge) ... 1 (right edge)
    var pan: CGFloat = 0
    @StateObject private var model = LiveModel()

    var body: some View {
        // Color.black takes exactly the offered size; the picture is overlaid and clipped to it,
        // so a filled (cropped) picture never makes this view wider than its slot
        Color.black
            .overlay {
                if let still = model.still {
                    GeometryReader { geo in
                        // Always laid out as "fit" and zoomed with a scale, so fit <-> fill is a
                        // smooth animatable change instead of a jump between content modes
                        let aspect = still.size.width / max(1, still.size.height)
                        let fitWidth = min(geo.size.width, geo.size.height * aspect)
                        let fitHeight = fitWidth / aspect
                        let fillScale = max(geo.size.width / fitWidth, geo.size.height / max(1, fitHeight))
                        let overflow = max(0, fitWidth * fillScale - geo.size.width)
                        Image(uiImage: still)
                            .resizable()
                            .aspectRatio(contentMode: .fit)
                            .frame(width: geo.size.width, height: geo.size.height)
                            .scaleEffect(fill ? fillScale : 1)
                            .offset(x: fill ? -pan * overflow / 2 : 0)
                    }
                }
            }
            .overlay {
                if let player = model.player {
                    VideoPlayer(player: player)
                        .aspectRatio(16 / 9, contentMode: fill ? .fill : .fit)
                        .opacity(model.video ? 1 : 0)
                        .allowsHitTesting(false)
                }
            }
            .overlay {
                if model.still == nil && !model.video {
                    ProgressView()
                }
            }
            .clipped()
        .task(id: camera.id) {
            await model.run(camera, client: client, width: frameWidth, transport: transport)
        }
    }
}

/// "Person now" / "Seen 4 min ago" / "No one seen".
struct PersonStatus: View {
    let camera: HubCamera

    var body: some View {
        HStack(spacing: 5) {
            if camera.isAsleep {
                Image(systemName: "moon.fill").font(.system(size: 8)).foregroundStyle(.indigo)
            } else {
                Circle()
                    // Green = someone's there (matches the watch's green edge); gray idle; red offline
                    .fill(camera.person ? Color.green : (camera.connected ? Color.gray : Color.red))
                    .frame(width: 7, height: 7)
            }
            if camera.isAsleep {
                Text("Asleep")
            } else if camera.person {
                Text("Person now")
            } else if !camera.connected {
                Text("Offline")
            } else if let seen = camera.lastSeenDate {
                Text("Seen \(seen, style: .relative) ago")
            } else {
                Text("No one seen")
            }
        }
        .font(.caption2.weight(.medium))
        .lineLimit(1)
    }
}
