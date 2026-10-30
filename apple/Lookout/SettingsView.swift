import SwiftUI

/// Settings, laid out like the iOS Settings app: hub status, the shared Person Alerts switch,
/// then one row per camera leading to its own page.
struct SettingsView: View {
    @ObservedObject var store: CameraStore
    @ObservedObject private var prefs = Prefs.shared
    @Environment(\.dismiss) private var dismiss
    // -openCamera <id> at launch (screenshots / testing) opens that camera's page
    @State private var path: [String] = {
        let args = ProcessInfo.processInfo.arguments
        guard let i = args.firstIndex(of: "-openCamera"), i + 1 < args.count else { return [] }
        return [args[i + 1]]
    }()

    var body: some View {
        NavigationStack(path: $path) {
            List {
                Section {
                    HubHeader(store: store)
                }

                Section {
                    Toggle(isOn: Binding(get: { store.alerts.enabled }, set: { store.setAlerts($0) })) {
                        Label { Text("Person Alerts") } icon: { SettingsIcon("bell.badge.fill", .red) }
                    }
                } footer: {
                    Text("Pops up the camera when someone is at the door. Shared with Lookout on your PC and projector.")
                }

                Section {
                    ForEach(prefs.sorted(store.cameras)) { cam in
                        NavigationLink(value: cam.id) {
                            CameraRow(camera: cam, client: store.client,
                                      alerts: store.alerts.enabled && store.alerts.camera(cam.id),
                                      onWatch: prefs.showsOnWatch(cam.id))
                        }
                    }
                    .onMove { prefs.move(store.cameras, from: $0, to: $1) }
                } header: {
                    Text("Cameras")
                } footer: {
                    Text("Touch and hold a camera, then drag to change the order on your iPhone and Apple Watch.")
                }

                HubSection(store: store)

                SecuritySection()

                Section {
                    LabeledContent("Version", value: Self.version)
                    Link("Setup Guide", destination: PairingView.setupGuide)
                }
            }
            .listStyle(.insetGrouped)
            .navigationDestination(for: String.self) { id in
                CameraSettingsView(store: store, cameraID: id)
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }

    private static var version: String {
        let info = Bundle.main.infoDictionary
        return "\(info?["CFBundleShortVersionString"] as? String ?? "1.0") (\(info?["CFBundleVersion"] as? String ?? "-"))"
    }
}

/// The paired hub: how it's reached, the watch, and unpairing.
private struct HubSection: View {
    @ObservedObject var store: CameraStore
    @ObservedObject private var linkStore = LinkStore.shared
    @State private var watchNote: String?
    @State private var sending = false
    @State private var confirmUnpair = false

    var body: some View {
        Section {
            LabeledContent("Hub", value: linkStore.link?.name ?? "-")
            LabeledContent("Connection", value: HubRoute.isHome ? "Home network" : "Away")
            if Prefs.shared.watchAvailable {
                Button {
                    sending = true
                    Task {
                        do {
                            try await Prefs.shared.sendWatchInvite()
                            watchNote = "Sent. Open Lookout on your watch to finish."
                        } catch {
                            watchNote = error.localizedDescription
                        }
                        sending = false
                    }
                } label: {
                    HStack {
                        Label { Text("Set Up Apple Watch") } icon: { SettingsIcon("applewatch", .blue) }
                        Spacer()
                        if sending { ProgressView() }
                    }
                }
                .disabled(sending)
            }
            Button(role: .destructive) { confirmUnpair = true } label: {
                Label { Text("Unpair This iPhone") } icon: { SettingsIcon("link", .gray) }
            }
            .confirmationDialog("Unpair from \(linkStore.link?.name ?? "this hub")?", isPresented: $confirmUnpair,
                                titleVisibility: .visible) {
                Button("Unpair", role: .destructive) { Task { await Pairing.unpair() } }
            } message: {
                Text("This iPhone stops showing cameras and getting alerts until you pair it again.")
            }
        } header: {
            Text("Hub")
        } footer: {
            Text(watchNote ?? "At home Lookout talks to your hub directly, encrypted with the hub's own certificate. Away, it uses the hub's away address.")
        }
    }
}

private struct SecuritySection: View {
    @ObservedObject private var lock = AppLock.shared

    var body: some View {
        Section {
            Toggle(isOn: Binding(get: { lock.enabled }, set: { on in Task { await lock.setEnabled(on) } })) {
                Label { Text("Require \(lock.biometryName)") } icon: {
                    SettingsIcon(lock.biometryName == "Touch ID" ? "touchid" : "faceid", .green)
                }
            }
        } footer: {
            Text("Ask for \(lock.biometryName) each time Lookout opens. Alerts still arrive.")
        }
    }
}

/// App mark + whether the hub is answering, as the first row.
private struct HubHeader: View {
    @ObservedObject var store: CameraStore

    var body: some View {
        HStack(spacing: 14) {
            AppMark(size: 52)
            VStack(alignment: .leading, spacing: 3) {
                Text("Lookout").font(.title3.weight(.semibold))
                HStack(spacing: 6) {
                    Circle()
                        .fill(store.unreachable ? Color.red : Color.green)
                        .frame(width: 7, height: 7)
                    Text(store.unreachable ? "Hub not reachable"
                                           : "\(store.cameras.count) cameras · connected")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.vertical, 4)
    }
}

/// Thumbnail, name, status, and small badges for what's switched on.
private struct CameraRow: View {
    let camera: HubCamera
    let client: HubClient
    let alerts: Bool
    let onWatch: Bool

    var body: some View {
        HStack(spacing: 12) {
            CameraThumbnail(camera: camera, client: client)
                .frame(width: 72, height: 41)
                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            VStack(alignment: .leading, spacing: 3) {
                Text(camera.name).font(.body.weight(.medium))
                PersonStatus(camera: camera).foregroundStyle(.secondary)
            }
            Spacer(minLength: 8)
            HStack(spacing: 6) {
                Image(systemName: alerts ? "bell.fill" : "bell.slash")
                    .foregroundStyle(alerts ? AnyShapeStyle(.red) : AnyShapeStyle(.tertiary))
                Image(systemName: "applewatch")
                    .foregroundStyle(onWatch ? AnyShapeStyle(.blue) : AnyShapeStyle(.tertiary))
            }
            .font(.footnote)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("\(alerts ? "Alerts on" : "Alerts off"), \(onWatch ? "on Apple Watch" : "hidden on Apple Watch")")
        }
        .padding(.vertical, 2)
    }
}

/// One camera: live preview over a blurred backdrop of itself, controls on Liquid Glass.
struct CameraSettingsView: View {
    @ObservedObject var store: CameraStore
    @ObservedObject private var prefs = Prefs.shared
    let cameraID: String
    @Environment(\.scenePhase) private var phase

    private var camera: HubCamera? { store.cameras.first { $0.id == cameraID } }

    var body: some View {
        if let cam = camera {
            ScrollView {
                VStack(spacing: 20) {
                    LiveCameraView(camera: cam, client: store.client, frameWidth: 800,
                                   transport: .hls(variant: "phone"))
                        .aspectRatio(16 / 9, contentMode: .fit)
                        .clipShape(RoundedRectangle(cornerRadius: 24, style: .continuous))
                        .overlay(alignment: .bottomLeading) {
                            PersonStatus(camera: cam)
                                .foregroundStyle(.primary)
                                .padding(.horizontal, 12)
                                .padding(.vertical, 7)
                                .glassEffect(.regular, in: .capsule)
                                .padding(12)
                        }
                        .shadow(color: .black.opacity(0.35), radius: 20, y: 10)

                    GlassGroup {
                        GlassToggleRow(title: "Person Alerts", subtitle: store.alerts.enabled
                                           ? "Pop up this camera when someone is in view"
                                           : "Turned off for every camera in Settings",
                                       icon: SettingsIcon("bell.badge.fill", .red),
                                       isOn: Binding(get: { store.alerts.camera(cam.id) },
                                                     set: { store.setAlerts($0, camera: cam.id) }))
                            .disabled(!store.alerts.enabled)
                        Divider().padding(.leading, 58)
                        GlassToggleRow(title: "Show on Apple Watch", subtitle: "Include this camera in the watch app",
                                       icon: SettingsIcon("applewatch", .blue),
                                       isOn: Binding(get: { prefs.showsOnWatch(cam.id) },
                                                     set: { prefs.setShowsOnWatch(cam.id, $0) }))
                    }

                    Text("Alerts are shared with Lookout on your PC and projector.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 6)

                    VisitorsStrip(camera: cam, client: store.client)
                        .padding(.top, 6)
                }
                .padding(.horizontal, 16)
                .padding(.top, 8)
                .padding(.bottom, 32)
            }
            .background {
                // The camera itself, heavily blurred, so the glass has something to refract
                CameraThumbnail(camera: cam, client: store.client)
                    .scaledToFill()
                    .blur(radius: 60)
                    .overlay(Color.black.opacity(0.35))
                    .ignoresSafeArea()
            }
            .navigationTitle(cam.name)
            .navigationBarTitleDisplayMode(.inline)
        } else {
            ContentUnavailableView("Camera unavailable", systemImage: "video.slash")
        }
    }
}

/// Rows stacked on one Liquid Glass card.
private struct GlassGroup<Content: View>: View {
    @ViewBuilder var content: Content

    var body: some View {
        VStack(spacing: 0) { content }
            .glassEffect(.regular, in: .rect(cornerRadius: 26))
    }
}

private struct GlassToggleRow: View {
    let title: String
    let subtitle: String
    let icon: SettingsIcon
    @Binding var isOn: Bool

    var body: some View {
        Toggle(isOn: $isOn) {
            HStack(spacing: 14) {
                icon
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.body.weight(.medium))
                    Text(subtitle).font(.footnote).foregroundStyle(.secondary)
                }
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
    }
}

/// The colored rounded-square icon used by the Settings app.
struct SettingsIcon: View {
    let symbol: String
    let color: Color

    init(_ symbol: String, _ color: Color) {
        self.symbol = symbol
        self.color = color
    }

    var body: some View {
        Image(systemName: symbol)
            .font(.system(size: 15, weight: .semibold))
            .foregroundStyle(.white)
            .frame(width: 30, height: 30)
            .background(color.gradient, in: RoundedRectangle(cornerRadius: 7, style: .continuous))
    }
}

/// A still from the hub, refreshed every few seconds while on screen.
struct CameraThumbnail: View {
    let camera: HubCamera
    let client: HubClient
    @State private var image: UIImage?

    var body: some View {
        ZStack {
            Color(white: 0.12)
            if let image {
                Image(uiImage: image).resizable().scaledToFill()
            }
        }
        .task(id: camera.id) {
            while !Task.isCancelled {
                if let data = try? await client.frame(camera, width: 320), let img = UIImage(data: data) {
                    withAnimation(.easeOut(duration: 0.25)) { image = img }
                }
                try? await Task.sleep(nanoseconds: 4_000_000_000)
            }
        }
    }
}
