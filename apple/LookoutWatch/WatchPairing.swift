import SwiftUI
import WatchConnectivity
import WatchKit
import WidgetKit

/// The watch pairs through the iPhone: the iPhone gets a one-time code from the hub and hands it
/// over, then the watch makes its own key and registers it with the hub itself.
@MainActor
final class WatchPairing: ObservableObject {
    static let shared = WatchPairing()
    enum State: Equatable { case idle, waiting, pairing, failed(String) }
    @Published var state = State.idle

    /// An invite arrived from the iPhone (lookout://pair?...).
    func accept(_ invite: String) {
        guard let url = URL(string: invite), let invite = PairingInvite(url: url) else { return }
        guard state != .pairing else { return }
        state = .pairing
        Task {
            do {
                try await Pairing.pair(invite, deviceName: WKInterfaceDevice.current().name, platform: "watchos")
                state = .idle
                WidgetCenter.shared.reloadAllTimelines()
                WKApplication.shared().registerForRemoteNotifications()   // hand the push token to the hub
            } catch {
                state = .failed(error.localizedDescription)
            }
        }
    }

    /// Ask the iPhone for an invite (wakes the iPhone app in the background if it can).
    func request() {
        let session = WCSession.default
        guard session.activationState == .activated, session.isReachable else {
            state = .waiting
            return
        }
        state = .waiting
        session.sendMessage(["needInvite": true], replyHandler: { reply in
            Task { @MainActor in
                if let invite = reply["invite"] as? String {
                    WatchPairing.shared.accept(invite)
                } else if let error = reply["error"] as? String {
                    WatchPairing.shared.state = .failed(error)
                }
            }
        }, errorHandler: { _ in
            Task { @MainActor in WatchPairing.shared.state = .waiting }
        })
    }
}

/// Shown until the watch is paired.
struct WatchSetupView: View {
    @ObservedObject private var pairing = WatchPairing.shared

    var body: some View {
        ScrollView {
            VStack(spacing: 10) {
                Image(systemName: "iphone.and.arrow.forward")
                    .font(.system(size: 30, weight: .semibold))
                    .foregroundStyle(.tint)
                switch pairing.state {
                case .pairing:
                    ProgressView()
                    Text("Connecting to your hub…").font(.footnote)
                case .failed(let why):
                    Text(why).font(.footnote).multilineTextAlignment(.center)
                    Button("Try Again") { pairing.request() }
                default:
                    Text("Set up on iPhone").font(.headline)
                    Text("Open Lookout on your iPhone. Once it's connected to your hub, this watch connects too.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                    Button("Ask iPhone Again") { pairing.request() }
                        .padding(.top, 4)
                }
            }
            .padding(.horizontal, 6)
        }
        .onAppear { pairing.request() }
    }
}
