import ActivityKit
import Foundation

/// Hands the hub what it needs to show "someone's at the door" as a Live Activity: the
/// push-to-start token, and the update token of each activity it starts (so it can end it).
enum LiveActivities {
    @MainActor private static var started = false

    @MainActor static func start() {
        guard !started, ActivityAuthorizationInfo().areActivitiesEnabled else { return }
        started = true
        Task {
            for await token in Activity<PersonActivityAttributes>.pushToStartTokenUpdates {
                await register(token, kind: "start")
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

    private static func register(_ token: Data, kind: String, camera: String? = nil) async {
        for delay in [0, 5, 30] as [UInt64] {
            try? await Task.sleep(nanoseconds: delay * 1_000_000_000)
            if (try? await HubClient().registerActivityToken(token, kind: kind, camera: camera)) != nil { return }
        }
    }
}
