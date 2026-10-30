import Foundation

/// Which camera a tapped notification asked for; WatchHome switches to it.
@MainActor
final class AlertRouter: ObservableObject {
    static let shared = AlertRouter()
    @Published var requested: String?
    nonisolated func open(_ camera: String) {
        Task { @MainActor in self.requested = camera }
    }
}
