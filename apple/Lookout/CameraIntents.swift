import AppIntents

/// Shortcuts and automations ("when I arrive home, wake the bedroom camera").
struct CameraEntity: AppEntity {
    static let typeDisplayRepresentation: TypeDisplayRepresentation = "Camera"
    static let defaultQuery = CameraQuery()

    let id: String
    let name: String

    var displayRepresentation: DisplayRepresentation { DisplayRepresentation(title: "\(name)") }
}

struct CameraQuery: EntityQuery {
    /// Cameras that can sleep, from the paired hub
    private func all() async throws -> [CameraEntity] {
        try await HubClient().cameras().cameras.filter { $0.can("sleep") }.map { CameraEntity(id: $0.id, name: $0.name) }
    }

    func entities(for identifiers: [String]) async throws -> [CameraEntity] {
        try await all().filter { identifiers.contains($0.id) }
    }

    func suggestedEntities() async throws -> [CameraEntity] { try await all() }
}

enum CameraIntentError: Error, CustomLocalizedStringResourceConvertible {
    case gone
    var localizedStringResource: LocalizedStringResource { "That camera isn't on your hub any more." }
}

private func setAsleep(_ entity: CameraEntity, _ asleep: Bool) async throws {
    let client = HubClient()
    guard let cam = try await client.cameras().cameras.first(where: { $0.id == entity.id }) else {
        throw CameraIntentError.gone
    }
    try await client.setAsleep(cam, asleep)
}

struct SleepCameraIntent: AppIntent {
    static let title: LocalizedStringResource = "Put Camera to Sleep"
    static let description = IntentDescription("Turns a camera's video off until it's woken.")

    @Parameter(title: "Camera") var camera: CameraEntity

    func perform() async throws -> some IntentResult & ProvidesDialog {
        try await setAsleep(camera, true)
        return .result(dialog: "\(camera.name) is asleep.")
    }
}

struct WakeCameraIntent: AppIntent {
    static let title: LocalizedStringResource = "Wake Camera"
    static let description = IntentDescription("Turns a sleeping camera back on. Its video is back in about a minute.")

    @Parameter(title: "Camera") var camera: CameraEntity

    func perform() async throws -> some IntentResult & ProvidesDialog {
        try await setAsleep(camera, false)
        return .result(dialog: "Waking \(camera.name). Video is back in about a minute.")
    }
}

struct LookoutShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: SleepCameraIntent(), phrases: ["Put a camera to sleep in \(.applicationName)"],
                    shortTitle: "Sleep Camera", systemImageName: "moon.zzz")
        AppShortcut(intent: WakeCameraIntent(), phrases: ["Wake a camera in \(.applicationName)"],
                    shortTitle: "Wake Camera", systemImageName: "sun.max")
    }
}
