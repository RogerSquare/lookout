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

    var body: some View {
        Group {
            if linkStore.link == nil {
                PairingView()
            } else {
                PhoneHome()
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
        }
        .onChange(of: phase) { _, p in
            if p == .background { lock.lockIfNeeded() }
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

/// Every camera live, stacked; tap one for full screen.
struct PhoneHome: View {
    @StateObject private var store = CameraStore()
    @StateObject private var browser = HubBrowser()       // follows the hub on the home network
    @Environment(\.scenePhase) private var phase
    @State private var fullScreen: HubCamera?
    @ObservedObject private var router = AlertRouter.shared
    // -openSettings at launch (screenshots / testing) opens straight into Settings
    @State private var showSettings = ProcessInfo.processInfo.arguments.contains("-openSettings")

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 16) {
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
                    }
                    ForEach(Prefs.shared.sorted(store.cameras)) { cam in
                        Button { fullScreen = cam } label: { CameraCard(camera: cam, client: store.client,
                                                                        live: phase == .active && fullScreen == nil) }
                            .buttonStyle(.plain)
                    }
                }
                .padding()
            }
            .navigationTitle("Lookout")
            .toolbar {
                Button { showSettings = true } label: { Image(systemName: "gearshape") }
                    .accessibilityLabel("Settings")
            }
        }
        .sheet(isPresented: $showSettings) {
            SettingsView(store: store)
        }
        .fullScreenCover(item: $fullScreen) { cam in
            ZStack(alignment: .topTrailing) {
                LiveCameraView(camera: cam, client: store.client, frameWidth: 1280, transport: .hls(variant: "phone"))
                    .ignoresSafeArea()
                Button { fullScreen = nil } label: {
                    Image(systemName: "xmark.circle.fill").font(.title).symbolRenderingMode(.hierarchical)
                }
                .padding()
            }
            .background(.black)
        }
        // Opened from a person alert: show that camera full screen once the list has it
        .onChange(of: router.requested) { _, id in openRequested(id) }
        .onChange(of: store.cameras) { _, _ in openRequested(router.requested) }
        .onAppear { store.start(); browser.start() }
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
}

extension PhoneHome {
    fileprivate func openRequested(_ id: String?) {
        guard let id, let cam = store.cameras.first(where: { $0.id == id }) else { return }
        showSettings = false
        fullScreen = cam
        router.requested = nil
    }
}

struct CameraCard: View {
    let camera: HubCamera
    let client: HubClient
    let live: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Group {
                if live {
                    LiveCameraView(camera: camera, client: client, frameWidth: 800, transport: .hls(variant: "phone"))
                } else {
                    Color.black
                }
            }
            .aspectRatio(16 / 9, contentMode: .fit)
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
            HStack {
                Text(camera.name).font(.headline)
                Spacer()
                PersonStatus(camera: camera).foregroundStyle(.secondary)
            }
            .padding(.horizontal, 4)
        }
    }
}
