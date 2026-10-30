import SwiftUI
import WatchKit
import WidgetKit

@main
struct LookoutWatchApp: App {
    @WKApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    init() {
        VideoGuard.checkLastRun()
        _ = Prefs.shared            // start WatchConnectivity so the iPhone's settings arrive
    }

    var body: some Scene {
        WindowGroup {
            WatchHome()
        }
        // Person alerts (category set by the hub): show the detection frame in the long look
        WKNotificationScene(controller: PersonNotificationController.self, category: "LOOKOUT_PERSON")
    }
}

/// One camera at a time, front door first. Swipe up/down for the next camera; the Digital
/// Crown zooms. Paging is done here rather than with a vertical-page TabView: that TabView also
/// claims the crown and takes it back whenever its pages are rebuilt (e.g. when a setting
/// arrives from the iPhone), which left zoom dead. The crown lives on this outer view, whose
/// identity never changes. Only the camera on screen streams, and nothing while the screen is off.
struct WatchHome: View {
    @StateObject private var store = CameraStore()
    @ObservedObject private var prefs = Prefs.shared
    @ObservedObject private var router = AlertRouter.shared
    @ObservedObject private var linkStore = LinkStore.shared
    @Environment(\.scenePhase) private var phase
    @State private var selection: String?
    @State private var direction: Edge = .bottom     // where the incoming camera slides from
    @State private var zoom = 0.0                    // crown: 0 = whole picture, 1 = full screen
    @State private var filled = false                // follows `zoom`, always through the spring
    @FocusState private var crownFocused: Bool

    private let spring = Animation.spring(response: 0.38, dampingFraction: 0.82)

    /// Cameras not hidden from the watch in the iPhone app's settings
    private var cameras: [HubCamera] { prefs.sorted(store.cameras).filter { prefs.showsOnWatch($0.id) } }

    private var current: HubCamera? {
        cameras.first { $0.id == selection } ?? cameras.first
    }

    var body: some View {
        ZStack {
            Color.black
            if linkStore.link == nil || store.unauthorized {
                WatchSetupView()
            } else if !store.cameras.isEmpty && cameras.isEmpty {
                VStack(spacing: 8) {
                    Image(systemName: "eye.slash").font(.title2)
                    Text("All cameras are hidden. Choose some in Lookout on your iPhone.")
                        .font(.footnote).multilineTextAlignment(.center)
                }
                .padding()
            } else if let cam = current {
                CameraPage(camera: cam, client: store.client, live: phase == .active, filled: filled,
                           position: (cameras.firstIndex(of: cam) ?? 0, cameras.count),
                           onTap: { setFilled(!filled) },
                           onSwipe: { step($0) })
                    .id(cam.id)
                    .transition(.asymmetric(insertion: .move(edge: direction),
                                            removal: .move(edge: direction == .bottom ? .top : .bottom)))
            } else {
                VStack(spacing: 8) {
                    if store.unreachable {
                        Image(systemName: "wifi.exclamationmark").font(.title2)
                        Text("Can't reach Lookout").font(.footnote).multilineTextAlignment(.center)
                    } else {
                        ProgressView()
                    }
                }
            }
        }
        .ignoresSafeArea()
        .focusable()
        .focused($crownFocused)
        .digitalCrownRotation($zoom, from: 0, through: 1, by: 1, sensitivity: .low,
                              isContinuous: false, isHapticFeedbackEnabled: true)
        .onChange(of: zoom) { _, z in setFilled(z >= 0.5) }
        // Someone just appeared: alert haptic (the strong "notification" tap) and bring that
        // camera up. Push notifications cover the app being closed; this covers it being open.
        .onChange(of: Set(cameras.filter(\.person).map(\.id))) { before, now in
            let new = now.subtracting(before)
            guard let id = cameras.first(where: { new.contains($0.id) })?.id else { return }
            WKInterfaceDevice.current().play(.notification)
            if id != current?.id {
                direction = .bottom
                zoom = 0
                filled = false
                withAnimation(spring) { selection = id }
            }
        }
        // Opened from the complication / widget: lookout://camera/<id>
        .onOpenURL { url in
            if url.scheme == "lookout", url.host == "camera", let id = url.pathComponents.last, id != "/" {
                router.open(id)
            }
        }
        // Opened from a person-alert notification: go to that camera
        .onChange(of: router.requested) { _, id in
            guard let id else { return }
            zoom = 0
            filled = false
            selection = id
            router.requested = nil
        }
        // A camera hidden from the iPhone while it was on screen: fall back to one still shown
        .onChange(of: cameras.map(\.id)) { _, ids in
            if let sel = selection, !ids.contains(sel) { selection = ids.first }
        }
        .onAppear {
            store.start()
            crownFocused = true
        }
        .onChange(of: phase) { _, p in
            if p == .active {
                store.start()
                crownFocused = true
            } else {
                store.stop()
                VideoGuard.leftNormally()
                WidgetCenter.shared.reloadAllTimelines()   // leave the complication current
            }
        }
    }

    private func setFilled(_ fill: Bool) {
        if (zoom >= 0.5) != fill { zoom = fill ? 1 : 0 }
        guard fill != filled else { return }
        withAnimation(spring) { filled = fill }
    }

    /// +1 = next camera (swipe up), -1 = previous (swipe down). Zooms back out on the way.
    private func step(_ delta: Int) {
        guard let cur = current, let i = cameras.firstIndex(of: cur) else { return }
        let j = i + delta
        guard cameras.indices.contains(j) else { return }
        direction = delta > 0 ? .bottom : .top
        zoom = 0
        filled = false
        withAnimation(spring) { selection = cameras[j].id }
        WKInterfaceDevice.current().play(.click)
    }
}

/// One camera. Whole picture on a rounded card (like the PC popup); full screen when `filled`.
/// Tap toggles; while filled, swipe left/right to look around the wide picture.
struct CameraPage: View {
    let camera: HubCamera
    let client: HubClient
    let live: Bool
    let filled: Bool
    let position: (index: Int, count: Int)      // for the page indicator under the name
    let onTap: () -> Void
    let onSwipe: (Int) -> Void
    @State private var pan: CGFloat = 0          // -1 ... 1 while filled
    @State private var panAtDragStart: CGFloat = 0
    @State private var appeared = false

    private let spring = Animation.spring(response: 0.38, dampingFraction: 0.82)
    private var person: Bool { camera.person }

    var body: some View {
        GeometryReader { geo in
            let size = geo.size
            // Whole-picture card: inset like the PC popup, 16:9, rounded, sitting above the label
            let cardWidth = size.width - 12
            let cardHeight = cardWidth * 9 / 16
            let radius: CGFloat = filled ? 0 : 16
            // How far a filled 16:9 picture overhangs the screen sideways (what panning reveals)
            let overflow = max(1, size.height * 16 / 9 - size.width)

            ZStack {
                Color.black
                Group {
                    if live {
                        LiveCameraView(camera: camera, client: client, frameWidth: 400, fill: filled,
                                       transport: .mjpeg(fps: 10), pan: pan)
                    } else {
                        Color.black
                    }
                }
                .frame(width: filled ? size.width : cardWidth, height: filled ? size.height : cardHeight)
                .clipShape(RoundedRectangle(cornerRadius: radius, style: .continuous))
                // Edge: a hairline normally, green with a glow while a person is in view
                .overlay(RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .strokeBorder(person ? Color.green : .white.opacity(filled ? 0 : 0.2),
                                  lineWidth: person ? 2.5 : 1)
                    .opacity(filled ? 0 : 1))
                .shadow(color: person ? .green.opacity(0.55) : .black.opacity(filled ? 0 : 0.6),
                        radius: person ? 14 : 10, y: person ? 0 : 4)
                .position(x: size.width / 2, y: filled ? size.height / 2 : size.height * 0.42)
                .scaleEffect(appeared ? 1 : 0.8)    // grows in like the PC popup
                .opacity(appeared ? 1 : 0)

                // Full screen: the green shows as a ring around the display instead
                RoundedRectangle(cornerRadius: 40, style: .continuous)
                    .strokeBorder(Color.green, lineWidth: 4)
                    .shadow(color: .green.opacity(0.7), radius: 8)
                    .opacity(filled && person ? 1 : 0)
                    .allowsHitTesting(false)

                VStack(alignment: .leading, spacing: 2) {
                    HStack(alignment: .firstTextBaseline) {
                        Text(camera.name).font(.headline).lineLimit(1)
                        Spacer(minLength: 6)
                        // Which camera this is: a short dash for the current one, like iOS pages
                        if position.count > 1 {
                            PageIndicator(index: position.index, count: position.count)
                        }
                    }
                    PersonStatus(camera: camera).foregroundStyle(.secondary)
                }
                .frame(width: cardWidth - 8, alignment: .leading)
                .position(x: size.width / 2, y: size.height * 0.42 + cardHeight / 2 + 26)
                .opacity(filled ? 0 : 1)            // full screen is just the picture
            }
            .contentShape(Rectangle())
            .onTapGesture(perform: onTap)
            // One drag gesture: mostly sideways while filled pans; mostly up/down changes camera
            .gesture(DragGesture(minimumDistance: 10)
                .onChanged { v in
                    guard filled, abs(v.translation.width) > abs(v.translation.height) else { return }
                    pan = min(1, max(-1, panAtDragStart - v.translation.width * 2 / overflow))
                }
                .onEnded { v in
                    panAtDragStart = pan
                    let dy = v.predictedEndTranslation.height
                    if abs(v.translation.height) > abs(v.translation.width), abs(dy) > 40 {
                        onSwipe(dy < 0 ? 1 : -1)
                    }
                })
        }
        .animation(.easeInOut(duration: 0.6), value: person)
        .onChange(of: filled) { _, isFilled in
            if !isFilled {
                withAnimation(spring) { pan = 0 }
                panAtDragStart = 0
            }
        }
        .onAppear { withAnimation(spring) { appeared = true } }
    }
}

/// Small horizontal page indicator that sits beside the camera name instead of on the picture.
/// Many cameras: shows "3 / 7" so it never crowds the name.
struct PageIndicator: View {
    let index: Int
    let count: Int

    var body: some View {
        if count > 5 {
            Text("\(index + 1)/\(count)")
                .font(.caption2.monospacedDigit().weight(.medium))
                .foregroundStyle(.secondary)
        } else {
            HStack(spacing: 3) {
                ForEach(0..<count, id: \.self) { i in
                    Capsule()
                        .fill(i == index ? Color.white : Color.white.opacity(0.3))
                        .frame(width: i == index ? 10 : 5, height: 5)
                }
            }
            .animation(.spring(response: 0.3, dampingFraction: 0.8), value: index)
            .accessibilityLabel("Camera \(index + 1) of \(count)")
        }
    }
}
