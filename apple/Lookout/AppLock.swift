import LocalAuthentication
import SwiftUI

/// Optional Face ID (or Touch ID / passcode) before the cameras show. Locks again whenever the
/// app goes to the background, so the app switcher never shows a camera either.
@MainActor
final class AppLock: ObservableObject {
    static let shared = AppLock()
    private static let key = "requireFaceID"

    @Published private(set) var enabled = UserDefaults.standard.bool(forKey: AppLock.key)
    @Published private(set) var locked = UserDefaults.standard.bool(forKey: AppLock.key)
    private var unlocking = false

    var biometryName: String {
        let ctx = LAContext()
        _ = ctx.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: nil)
        switch ctx.biometryType {
        case .faceID: return "Face ID"
        case .touchID: return "Touch ID"
        case .opticID: return "Optic ID"
        default: return "Passcode"
        }
    }

    func lockIfNeeded() {
        if enabled { locked = true }
    }

    func unlock() async {
        guard locked, !unlocking else { return }
        if await authenticate("Unlock Lookout to see your cameras") { locked = false }
    }

    /// Turning it on asks once, so nobody locks themselves out of a lock they can't open.
    func setEnabled(_ on: Bool) async {
        guard on != enabled else { return }
        guard await authenticate(on ? "Require \(biometryName) to open Lookout" : "Turn off \(biometryName) for Lookout") else { return }
        enabled = on
        UserDefaults.standard.set(on, forKey: Self.key)
    }

    private func authenticate(_ reason: String) async -> Bool {
        unlocking = true
        defer { unlocking = false }
        let ctx = LAContext()
        ctx.localizedFallbackTitle = "Use Passcode"
        return (try? await ctx.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason)) ?? false
    }
}

struct LockScreen: View {
    @ObservedObject private var lock = AppLock.shared

    var body: some View {
        ZStack {
            Rectangle().fill(.ultraThickMaterial).ignoresSafeArea()
            VStack(spacing: 18) {
                AppMark(size: 72)
                Text("Lookout is locked").font(.title2.weight(.semibold))
                Button {
                    Task { await lock.unlock() }
                } label: {
                    Label("Unlock with \(lock.biometryName)",
                          systemImage: lock.biometryName == "Touch ID" ? "touchid" : "faceid")
                        .font(.headline)
                        .padding(.horizontal, 8)
                }
                .buttonStyle(.glassProminent)
                .controlSize(.large)
            }
        }
        .task { await lock.unlock() }
    }
}
