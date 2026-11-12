import SwiftUI

@main
struct LookoutApp: App {
    @UIApplicationDelegateAdaptor(PhoneAppDelegate.self) private var appDelegate

    init() {
        VideoGuard.checkLastRun()
        _ = Prefs.shared            // start WatchConnectivity so settings reach the watch
    }

    var body: some Scene {
        WindowGroup {
            RootView()
        }
    }
}

/// Pairing until there's a hub, then the cameras; Face ID over everything when it's on.
struct RootView: View {
    @ObservedObject private var linkStore = LinkStore.shared
    @ObservedObject private var lock = AppLock.shared
    @ObservedObject private var pairing = PhonePairing.shared
    @Environment(\.scenePhase) private var phase

    /// -LookoutDemoCameras n (debug builds): fake cameras, no hub or pairing needed
    #if DEBUG
    private static let demo = Demo.enabled
    #else
    private static let demo = false
    #endif

    var body: some View {
        Group {
            if linkStore.link == nil && !Self.demo {
                PairingView()
            } else {
                PhoneTabs()
            }
        }
        .overlay {
            if lock.locked { LockScreen().transition(.opacity) }
        }
        .animation(.easeOut(duration: 0.2), value: lock.locked)
        // lookout://pair?... from the iPhone Camera app scanning the setup page's QR code
        .onOpenURL { url in
            if url.scheme == "lookout", url.host() == "pair" {
                Task { await pairing.open(url.absoluteString) }
            }
            // From a widget, the Live Activity or Control Center: open that camera
            if url.scheme == "lookout", url.host() == "camera", let id = url.pathComponents.dropFirst().first {
                AlertRouter.shared.open(id)
            }
            if url.scheme == "lookout", url.host() == "door" {
                Task {
                    let cams = (try? await HubClient().cameras().cameras) ?? []
                    if let cam = cams.first(where: { $0.role == "door" }) ?? cams.first { AlertRouter.shared.open(cam.id) }
                }
            }
        }
        .onChange(of: phase) { _, p in
            if p == .background { lock.lockIfNeeded() }
        }
        // -pairLink <lookout://pair?...> at launch (simulator testing, where nothing can scan)
        .task {
            let args = ProcessInfo.processInfo.arguments
            if let i = args.firstIndex(of: "-pairLink"), i + 1 < args.count {
                await pairing.open(args[i + 1])
            }
        }
        .alert("Switch to \(pairing.pendingSwitch?.name ?? "this hub")?",
               isPresented: Binding(get: { pairing.pendingSwitch != nil }, set: { if !$0 { pairing.pendingSwitch = nil } })) {
            Button("Switch Hub") {
                if let invite = pairing.pendingSwitch {
                    Task {
                        await Pairing.unpair()
                        _ = await pairing.pair(invite)
                    }
                }
                pairing.pendingSwitch = nil
            }
            Button("Cancel", role: .cancel) { pairing.pendingSwitch = nil }
        } message: {
            Text("This iPhone and its Apple Watch will show that hub's cameras instead of \(linkStore.link?.name ?? "the current hub").")
        }
    }
}

/// Live, Events and Settings, on a floating tab bar (hidden while a layout fills the screen).
struct PhoneTabs: View {
    enum Page: Hashable { case live, events, settings }

    @StateObject private var store = CameraStore()
    @ObservedObject private var router = AlertRouter.shared
    @ObservedObject private var sleepState = SleepState.shared
    @Environment(\.scenePhase) private var phase
    @State private var page: Page = ProcessInfo.processInfo.arguments.contains("-openEvents") ? .events : .live

    var body: some View {
        TabView(selection: $page) {
            Tab("Live", systemImage: "video.fill", value: Page.live) {
                PhoneHome(store: store, visible: page == .live)
            }
            Tab("Events", systemImage: "clock.fill", value: Page.events) {
                EventsView(store: store)
            }
            Tab("Settings", systemImage: "gearshape.fill", value: Page.settings) {
                SettingsView(store: store, showsDone: false)
            }
        }
        .alert("Couldn't change the camera", isPresented: Binding(get: { sleepState.error != nil },
                                                                   set: { if !$0 { sleepState.error = nil } })) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(sleepState.error ?? "")
        }
        // A person alert opens its camera on the Live tab
        .onChange(of: router.requested) { _, id in if id != nil { page = .live } }
        // Every tab needs the camera list, whichever one opens first
        .onAppear { store.start() }
        // Live Activities follow the cameras: none left over once they've gone
        .onChange(of: store.cameras) { _, cams in Task { await LiveActivities.reconcile(cams) } }
        .onChange(of: phase) { _, p in if p == .active { store.start() } else { store.stop() } }
    }
}

/// The cameras laid out the way the user picked (List, Grid or Focus), or a wall of every camera
/// when the phone is turned sideways. Tap one for full screen.
struct PhoneHome: View {
    @ObservedObject var store: CameraStore
    /// The Live tab is the one on screen (streams pause on the other tabs)
    var visible = true
    @StateObject private var browser = HubBrowser()       // follows the hub on the home network
    @ObservedObject private var prefs = Prefs.shared
    @Environment(\.scenePhase) private var phase
    /// The camera open in the full-screen player
    @State private var playerID: String?
    @ObservedObject private var router = AlertRouter.shared
    // -openSettings / -openArrange at launch (screenshots / testing) open those sheets;
    // -homeLayout list|grid|focus picks the layout (it's a stored setting, read from the arguments)
    @State private var showSettings = ProcessInfo.processInfo.arguments.contains("-openSettings")
    @State private var showArrange = ProcessInfo.processInfo.arguments.contains("-openArrange")
    #if DEBUG
    /// -openPlayer <camera id> at launch (screenshots / testing)
    private static let openPlayer: String? = {
        let args = ProcessInfo.processInfo.arguments
        guard let i = args.firstIndex(of: "-openPlayer"), i + 1 < args.count else { return nil }
        return args[i + 1]
    }()
    @State private var openedPlayer = false
    #endif
    @AppStorage("homeLayout") private var layout = HomeLayout.list
    @State private var focusedID: String?
    /// iPhone Duo: the hinge (nil on other iPhones), and the inner screen's shared live camera and
    /// Today list (one stream that carries on as the Duo folds between postures)
    @State private var hinge: HingeStatus?
    @State private var showWall = false
    @State private var duoShownID: String?
    @StateObject private var duoModel = LiveModel()
    @StateObject private var today = TodayEvents()

    /// The cameras the home screen shows, in the user's order
    private var cameras: [HubCamera] { prefs.onPhone(store.cameras) }
    /// Streams run only while the app is in front and no camera is full screen
    private var live: Bool { phase == .active && playerID == nil && visible }
    /// What the player swipes through: the home screen's cameras, or every camera when the one
    /// opened (from an alert) is hidden here
    private var playerCameras: [HubCamera] {
        cameras.contains { $0.id == playerID } ? cameras : prefs.sorted(store.cameras)
    }

    var body: some View {
        GeometryReader { geo in
            let posture = cameras.isEmpty ? .phone : HomePosture.resolve(size: geo.size, hinge: hinge, fold: geo.fold())
            let wall = posture == .phone && geo.size.width > geo.size.height && !cameras.isEmpty
            let fullBleed = wall || posture == .laptop || posture == .book
            NavigationStack {
                Group {
                    switch posture {
                    case .laptop:
                        DuoLaptop(cameras: cameras, client: store.client, focusedID: $focusedID, model: duoModel,
                                  today: today) { playerID = $0.id }
                    case .book:
                        DuoBook(cameras: cameras, client: store.client, live: live, focusedID: $focusedID,
                                model: duoModel, today: today, open: { playerID = $0.id }, showWall: { showWall = true })
                    case .open where layout == .focus:
                        DuoOpenFocus(cameras: cameras, client: store.client, live: live, focusedID: $focusedID,
                                     model: duoModel, today: today) { playerID = $0.id }
                    default:
                        if wall {
                            CameraWall(cameras: cameras, client: store.client, live: live) { playerID = $0.id }
                        } else {
                            ScrollView {
                                VStack(spacing: 16) {
                                    emptyState
                                    cameraLayout
                                }
                                .padding()
                            }
                        }
                    }
                }
                .navigationTitle("Lookout")
                .toolbar(fullBleed ? .hidden : .visible, for: .navigationBar)
                .toolbar(fullBleed ? .hidden : .visible, for: .tabBar)
                .toolbar { toolbar }
            }
            .statusBarHidden(fullBleed)
            // The inner screen's live camera: one stream, kept while the Duo folds and unfolds
            .task(id: duoStreamKey(posture)) {
                guard posture.usesDuoStream(layout), live,
                      let cam = cameras.first(where: { $0.id == focusedID }) ?? cameras.first(where: { !$0.isOnDemand })
                else { return }
                if duoShownID != cam.id {
                    duoModel.reset()             // another camera: drop the last one's picture
                    duoShownID = cam.id
                }
                await duoModel.run(cam, client: store.client, width: 1280, transport: cam.liveTransport(hls: "phone"))
            }
            .task(id: posture == .phone ? "" : cameras.map(\.id).joined(separator: ",")) {
                guard posture != .phone else { return }
                await today.run(cameras, client: store.client)
            }
        }
        .observingHinge($hinge)
        .debugScreen()
        .fullScreenCover(isPresented: $showWall) {
            CameraWall(cameras: cameras, client: store.client, live: phase == .active) { cam in
                showWall = false
                playerID = cam.id
            }
            .overlay(alignment: .topLeading) {
                Button { showWall = false } label: {
                    Image(systemName: "xmark").font(.body.weight(.semibold)).frame(width: 44, height: 44)
                }
                .buttonStyle(.plain)
                .glassEffect(.regular.interactive(), in: .circle)
                .accessibilityLabel("Close")
                .padding()
            }
            .background(.black)
        }
        #if DEBUG
        // -LookoutFakeLandscape: the sideways wall drawn turned on an upright screen (the
        // simulator can't be rotated over SSH); rotate the screenshot 90° to check it
        .overlay {
            if ProcessInfo.processInfo.arguments.contains("-LookoutFakeLandscape") {
                GeometryReader { geo in
                    let full = CGSize(width: geo.size.width + geo.safeAreaInsets.leading + geo.safeAreaInsets.trailing,
                                      height: geo.size.height + geo.safeAreaInsets.top + geo.safeAreaInsets.bottom)
                    CameraWall(cameras: cameras, client: store.client, live: live) { playerID = $0.id }
                        .frame(width: full.height, height: full.width)
                        .rotationEffect(.degrees(90))
                        .frame(width: full.width, height: full.height)
                }
                .ignoresSafeArea()
                .background(.black)
            }
        }
        #endif
        .sheet(isPresented: $showSettings) {
            SettingsView(store: store)
        }
        .sheet(isPresented: $showArrange) {
            ArrangeCamerasView(store: store)
        }
        .fullScreenCover(isPresented: Binding(get: { playerID != nil }, set: { if !$0 { playerID = nil } })) {
            if let id = playerID {
                CameraPlayer(cameras: playerCameras, client: store.client, startID: id) { playerID = nil }
            }
        }
        // Opened from a person alert: show that camera full screen once the list has it
        .onChange(of: router.requested) { _, id in openRequested(id) }
        .onChange(of: store.cameras) { _, _ in openRequested(router.requested) }
        .onAppear { store.start(); browser.start() }
        #if DEBUG
        .onChange(of: store.cameras) { _, cams in
            if let id = Self.openPlayer, !openedPlayer, cams.contains(where: { $0.id == id }) {
                openedPlayer = true
                playerID = id
            }
        }
        #endif
        .onChange(of: phase) { _, p in
            if p == .active {
                store.start()
                browser.start()
            } else {
                store.stop()
                browser.stop()
                VideoGuard.leftNormally()
            }
        }
    }

    @ViewBuilder private var emptyState: some View {
        if store.unauthorized {
            ContentUnavailableView {
                Label("This iPhone was removed", systemImage: "lock.slash")
            } description: {
                Text("The hub no longer accepts this iPhone. Pair it again from the hub's setup page.")
            } actions: {
                Button("Pair Again") { HubSession.shared.clear() }
                    .buttonStyle(.glassProminent)
            }
        } else if store.cameras.isEmpty {
            if store.unreachable {
                ContentUnavailableView("Can't reach your hub", systemImage: "wifi.exclamationmark",
                                       description: Text(HubSession.shared.link?.remoteURL == nil
                                           ? "Lookout reaches your hub on your home network. Connect to it and try again."
                                           : "Your hub isn't answering at home or through its away address."))
            } else {
                ProgressView().padding(.top, 80)
            }
        } else if cameras.isEmpty {
            ContentUnavailableView {
                Label("All cameras are hidden", systemImage: "eye.slash")
            } description: {
                Text("Choose which cameras show here. Hidden cameras still send alerts.")
            } actions: {
                Button("Arrange Cameras") { showArrange = true }
                    .buttonStyle(.glassProminent)
            }
        }
    }

    @ViewBuilder private var cameraLayout: some View {
        switch layout {
        case .list:
            ForEach(cameras) { cam in
                Button { playerID = cam.id } label: { CameraCard(camera: cam, client: store.client, live: live) }
                    .buttonStyle(.plain)
            }
        case .grid:
            CameraGrid(cameras: cameras, client: store.client, live: live) { playerID = $0.id }
        case .focus:
            CameraFocus(cameras: cameras, client: store.client, live: live, focusedID: $focusedID) { playerID = $0.id }
        }
    }

    @ToolbarContentBuilder private var toolbar: some ToolbarContent {
        ToolbarItem(placement: .topBarTrailing) {
            Menu {
                Picker("Layout", selection: $layout) {
                    ForEach(HomeLayout.allCases) { option in
                        Label(option.title, systemImage: option.symbol).tag(option)
                    }
                }
                Divider()
                Button { showArrange = true } label: {
                    Label("Arrange Cameras", systemImage: "arrow.up.arrow.down")
                }
                if store.cameras.contains(where: { $0.can("sleep") }) {
                    Divider()
                    Button {
                        Task { await SleepState.shared.setAll(store.cameras, asleep: true, client: store.client) }
                    } label: { Label("Sleep All Cameras", systemImage: "moon.zzz") }
                    Button {
                        Task { await SleepState.shared.setAll(store.cameras, asleep: false, client: store.client) }
                    } label: { Label("Wake All Cameras", systemImage: "sun.max") }
                }
            } label: {
                Image(systemName: layout.symbol)
            }
            .accessibilityLabel("Layout")
        }
    }
}

extension PhoneHome {
    /// Restart the Duo's stream only when the camera changes or it pauses, not on every fold
    fileprivate func duoStreamKey(_ posture: HomePosture) -> String {
        guard posture.usesDuoStream(layout), live else { return "off" }
        return (cameras.first(where: { $0.id == focusedID }) ?? cameras.first)?.id ?? "off"
    }

    fileprivate func openRequested(_ id: String?) {
        guard let id, let cam = store.cameras.first(where: { $0.id == id }) else { return }
        showSettings = false
        showArrange = false
        focusedID = id
        playerID = cam.id
        router.requested = nil
    }
}

/// The List layout's card: the picture, then the name and person status under it.
struct CameraCard: View {
    let camera: HubCamera
    let client: HubClient
    let live: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            CameraTile(camera: camera, client: client, live: live, frameWidth: 800, showsName: false)
                .aspectRatio(16 / 9, contentMode: .fit)
            HStack {
                Text(camera.name).font(.headline)
                Spacer()
                PersonStatus(camera: camera).foregroundStyle(.secondary)
            }
            .padding(.horizontal, 4)
            .accessibilityHidden(true)
        }
    }
}
