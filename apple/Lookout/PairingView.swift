import SwiftUI

/// Pairing on the iPhone: scan the hub's QR code, or pick a hub found on this network and type
/// its 6-digit code. Afterwards the watch is paired through the iPhone.
@MainActor
final class PhonePairing: ObservableObject {
    static let shared = PhonePairing()
    @Published var busy = false
    @Published var error: String?
    /// A lookout://pair link opened from outside (the iPhone Camera app) while already paired
    @Published var pendingSwitch: PairingInvite?

    func pair(_ invite: PairingInvite) async -> Bool {
        busy = true
        error = nil
        defer { busy = false }
        do {
            try await Pairing.pair(invite, deviceName: UIDevice.current.name, platform: "ios")
            UINotificationFeedbackGenerator().notificationOccurred(.success)
            Prefs.shared.push()
            try? await Prefs.shared.sendWatchInvite()       // the watch pairs itself in the background
            UIApplication.shared.registerForRemoteNotifications()
            return true
        } catch {
            UINotificationFeedbackGenerator().notificationOccurred(.error)
            self.error = error.localizedDescription
            return false
        }
    }

    /// A scanned or opened link.
    func open(_ text: String) async {
        guard let url = URL(string: text), let invite = PairingInvite(url: url) else {
            error = "That isn't a Lookout pairing code. On the hub's setup page, open Apps and choose Pair an app."
            return
        }
        if let link = HubSession.shared.link, link.hubID != invite.hubID {
            pendingSwitch = invite                 // confirm before replacing the current hub
            return
        }
        _ = await pair(invite)
    }
}

struct PairingView: View {
    @StateObject private var browser = HubBrowser()
    @ObservedObject private var pairing = PhonePairing.shared
    @State private var scanning = false
    @State private var codeFor: HubBrowser.Found?

    static let setupGuide = URL(string: "https://rogersquare.github.io/lookout/")!

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 28) {
                    VStack(spacing: 14) {
                        AppMark(size: 84)
                        Text("Connect to your hub")
                            .font(.largeTitle.weight(.bold))
                            .multilineTextAlignment(.center)
                        Text("Lookout shows the cameras on your Lookout Hub. On the hub's setup page, open Apps and choose Pair an app.")
                            .font(.body)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                    }
                    .padding(.top, 24)

                    Button {
                        scanning = true
                    } label: {
                        Label("Scan QR Code", systemImage: "qrcode.viewfinder")
                            .font(.headline)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 6)
                    }
                    .buttonStyle(.glassProminent)
                    .controlSize(.large)

                    VStack(alignment: .leading, spacing: 10) {
                        Text("On this network")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 6)
                        VStack(spacing: 0) {
                            if browser.hubs.isEmpty {
                                HStack(spacing: 12) {
                                    ProgressView()
                                    Text("Looking for hubs…").foregroundStyle(.secondary)
                                    Spacer()
                                }
                                .padding(16)
                            }
                            ForEach(browser.hubs) { hub in
                                Button { codeFor = hub } label: {
                                    HStack(spacing: 14) {
                                        SettingsIcon("server.rack", .indigo)
                                        VStack(alignment: .leading, spacing: 2) {
                                            Text(hub.name).font(.body.weight(.medium))
                                            Text(hub.address).font(.footnote).foregroundStyle(.secondary)
                                        }
                                        Spacer()
                                        Text("Enter code").font(.footnote).foregroundStyle(.tint)
                                        Image(systemName: "chevron.right").font(.footnote.weight(.semibold))
                                            .foregroundStyle(.tertiary)
                                    }
                                    .padding(.horizontal, 16)
                                    .padding(.vertical, 12)
                                    .contentShape(Rectangle())
                                }
                                .buttonStyle(.plain)
                                if hub != browser.hubs.last { Divider().padding(.leading, 60) }
                            }
                        }
                        .glassEffect(.regular, in: .rect(cornerRadius: 22))
                    }

                    if let error = pairing.error {
                        Label(error, systemImage: "exclamationmark.triangle.fill")
                            .font(.footnote)
                            .foregroundStyle(.red)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 6)
                    }

                    Link(destination: Self.setupGuide) {
                        Label("No hub yet? Set one up", systemImage: "book")
                            .font(.subheadline)
                    }
                    .padding(.top, 4)
                }
                .padding(.horizontal, 20)
                .padding(.bottom, 32)
            }
            .background(PairingBackdrop())
            .overlay {
                if pairing.busy {
                    VStack(spacing: 12) {
                        ProgressView().controlSize(.large)
                        Text("Connecting…").font(.headline)
                    }
                    .padding(28)
                    .glassEffect(.regular, in: .rect(cornerRadius: 24))
                }
            }
        }
        .onAppear { browser.start() }
        .onDisappear { browser.stop() }
        .sheet(isPresented: $scanning) {
            ScanSheet { text in
                scanning = false
                Task { await pairing.open(text) }
            }
        }
        .sheet(item: $codeFor) { hub in
            CodeEntrySheet(hub: hub) { code in
                codeFor = nil
                Task { _ = await pairing.pair(browser.invite(hub, code: code)) }
            }
            .presentationDetents([.medium])
        }
    }
}

/// The app's mark: a lens ring on deep blue.
struct AppMark: View {
    var size: CGFloat = 52

    var body: some View {
        RoundedRectangle(cornerRadius: size * 0.23, style: .continuous)
            .fill(LinearGradient(colors: [Color(red: 0.16, green: 0.36, blue: 0.86), Color(red: 0.07, green: 0.12, blue: 0.3)],
                                 startPoint: .top, endPoint: .bottom))
            .frame(width: size, height: size)
            .overlay(Image(systemName: "video.fill").font(.system(size: size * 0.42, weight: .semibold)).foregroundStyle(.white))
            .shadow(color: .black.opacity(0.25), radius: size * 0.15, y: size * 0.06)
    }
}

private struct PairingBackdrop: View {
    var body: some View {
        LinearGradient(colors: [Color(red: 0.10, green: 0.18, blue: 0.42).opacity(0.55), Color(.systemBackground)],
                       startPoint: .top, endPoint: .center)
            .ignoresSafeArea()
    }
}

private struct ScanSheet: View {
    let onCode: (String) -> Void
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        ZStack {
            QRScanner(onCode: onCode).ignoresSafeArea()
            RoundedRectangle(cornerRadius: 28, style: .continuous)
                .strokeBorder(.white.opacity(0.9), lineWidth: 3)
                .frame(width: 250, height: 250)
            VStack {
                HStack {
                    Spacer()
                    Button { dismiss() } label: {
                        Image(systemName: "xmark").font(.headline).padding(12)
                    }
                    .buttonStyle(.glass)
                    .accessibilityLabel("Close")
                }
                Spacer()
                Text("Point at the QR code on the hub's setup page")
                    .font(.callout.weight(.medium))
                    .padding(.horizontal, 18)
                    .padding(.vertical, 12)
                    .glassEffect(.regular, in: .capsule)
            }
            .padding(20)
        }
    }
}

private struct CodeEntrySheet: View {
    let hub: HubBrowser.Found
    let onCode: (String) -> Void
    @State private var code = ""
    @FocusState private var focused: Bool

    var body: some View {
        VStack(spacing: 18) {
            Text("Enter the code for \(hub.name)").font(.title3.weight(.semibold)).multilineTextAlignment(.center)
            Text("On the hub's setup page, open Apps and choose Pair an app. The 6-digit code is under the QR code.")
                .font(.footnote).foregroundStyle(.secondary).multilineTextAlignment(.center)
            TextField("000 000", text: $code)
                .font(.system(size: 40, weight: .bold, design: .rounded).monospacedDigit())
                .multilineTextAlignment(.center)
                .keyboardType(.numberPad)
                .textContentType(.oneTimeCode)
                .focused($focused)
                .onChange(of: code) { _, c in
                    let digits = String(c.filter(\.isNumber).prefix(6))
                    if digits != c { code = digits }
                    if digits.count == 6 { onCode(digits) }
                }
                .padding(.vertical, 8)
                .glassEffect(.regular, in: .rect(cornerRadius: 18))
            Button("Connect") { onCode(code) }
                .buttonStyle(.glassProminent)
                .controlSize(.large)
                .disabled(code.count != 6)
        }
        .padding(24)
        .onAppear { focused = true }
    }
}
