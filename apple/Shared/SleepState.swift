import Foundation

/// Sleeping and waking cameras from anywhere in the app: remembers which ones are waking up (a
/// camera takes about a minute to send video again), so tiles can show a spinner meanwhile.
@MainActor
final class SleepState: ObservableObject {
    static let shared = SleepState()
    /// How long a woken camera takes to send video again (measured: about 55 s)
    static let wakeTime: TimeInterval = 60

    @Published private(set) var wakingUntil: [String: Date] = [:]
    @Published private(set) var busy: Set<String> = []
    @Published var error: String?

    func isWaking(_ cam: HubCamera) -> Bool {
        guard let until = wakingUntil[cam.id], until > Date() else { return false }
        return !cam.connected || cam.isAsleep
    }

    func set(_ cam: HubCamera, asleep: Bool, client: HubClient) async {
        guard !busy.contains(cam.id) else { return }
        busy.insert(cam.id)
        defer { busy.remove(cam.id) }
        do {
            try await client.setAsleep(cam, asleep)
            wakingUntil[cam.id] = asleep ? nil : Date().addingTimeInterval(Self.wakeTime)
        } catch {
            self.error = "\(cam.name): \(error.localizedDescription)"
        }
    }

    /// Every camera that can sleep, one after another (the hub spaces commands to a camera).
    func setAll(_ cams: [HubCamera], asleep: Bool, client: HubClient) async {
        for cam in cams where cam.can("sleep") && cam.isAsleep != asleep {
            await set(cam, asleep: asleep, client: client)
        }
    }
}
