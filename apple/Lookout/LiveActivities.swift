import ActivityKit
import Foundation

/// Hands the hub what it needs to show "someone's at the door" as a Live Activity: the
/// push-to-start token, and the update token of each activity it starts (so it can end it).
enum LiveActivities {
    @MainActor private static var started = false
    @MainActor private static var startToken: Data?
    /// Off unless the user turns it on (Settings): the hub only starts one for a device that
    /// handed it its push-to-start token
    static let enabledKey = "liveActivityAtDoor"
    static var enabled: Bool { UserDefaults.standard.bool(forKey: enabledKey) }

    @MainActor static func start() {
        guard !started, ActivityAuthorizationInfo().areActivitiesEnabled else { return }
        started = true
        Task {
            for await token in Activity<PersonActivityAttributes>.pushToStartTokenUpdates {
                startToken = token
                await register(token, kind: enabled ? "start" : "stop")
            }
        }
        Task {
            for await activity in Activity<PersonActivityAttributes>.activityUpdates {
                let camera = activity.attributes.cameraID
                Task {
                    for await token in activity.pushTokenUpdates {
                        await register(token, kind: "update", camera: camera)
                    }
                }
            }
        }
    }

    /// The Settings switch changed: tell the hub, and clear any showing now if it's off.
    @MainActor static func setEnabled(_ on: Bool) {
        UserDefaults.standard.set(on, forKey: enabledKey)
        Task {
            if let token = startToken { await register(token, kind: on ? "start" : "stop") }
            if !on {
                for activity in Activity<PersonActivityAttributes>.activities {
                    await activity.end(nil, dismissalPolicy: .immediate)
                }
            }
        }
    }

    /// End this camera's activities right away (they left).
    static func end(camera: String) async {
        for activity in Activity<PersonActivityAttributes>.activities where activity.attributes.cameraID == camera {
            await activity.end(nil, dismissalPolicy: .immediate)
        }
    }

    /// Keep only what's true now: no activity for a camera with nobody in view, and never more
    /// than one per camera. Run whenever the app has a fresh camera list.
    static func reconcile(_ cameras: [HubCamera]) async {
        var seen = Set<String>()
        let byID = Dictionary(uniqueKeysWithValues: cameras.map { ($0.id, $0) })
        for activity in Activity<PersonActivityAttributes>.activities.sorted(by: { $0.content.state.since > $1.content.state.since }) {
            let id = activity.attributes.cameraID
            if byID[id]?.person != true || seen.contains(id) {
                await activity.end(nil, dismissalPolicy: .immediate)
            } else {
                seen.insert(id)
            }
        }
    }

    private static func register(_ token: Data, kind: String, camera: String? = nil) async {
        for delay in [0, 5, 30] as [UInt64] {
            try? await Task.sleep(nanoseconds: delay * 1_000_000_000)
            if (try? await HubClient().registerActivityToken(token, kind: kind, camera: camera)) != nil { return }
        }
    }
}
