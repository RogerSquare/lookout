#if os(iOS)
import ActivityKit
import Foundation

/// The Live Activity while someone is at a door camera. The hub starts it with a push (property
/// names match its JSON) and ends it when they leave.
struct PersonActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        /// Someone is there right now
        var person: Bool
        /// When they arrived / left (unix seconds)
        var since: Double
        var gone: Double?

        var sinceDate: Date { Date(timeIntervalSince1970: since) }
        var goneDate: Date? { gone.map { Date(timeIntervalSince1970: $0) } }
    }

    var cameraID: String
    var name: String
}
#endif
