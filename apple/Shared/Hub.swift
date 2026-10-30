import Foundation

/// A camera as the Lookout Hub's watch API reports it (GET /api/watch/cameras).
struct HubCamera: Codable, Identifiable, Equatable {
    let id: String
    let name: String
    let connected: Bool
    let person: Bool
    let since: String?
    let lastSeen: String?
    let alerts: Bool
    let frame: String
    let hls: String?
    let mjpeg: String?

    enum CodingKeys: String, CodingKey {
        case id, name, connected, person, since, alerts, frame, hls, mjpeg
        case lastSeen = "last_seen"
    }

    var lastSeenDate: Date? { lastSeen.flatMap(Self.parse) }

    private static func parse(_ s: String) -> Date? {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f.date(from: s)
    }
}

/// The hub's shared alerts switch: everything on/off, plus each camera's own switch.
struct HubAlerts: Codable, Equatable {
    var enabled: Bool
    var cameras: [String: Bool]

    func camera(_ id: String) -> Bool { cameras[id] ?? true }
}

struct CameraList: Codable {
    let alerts: HubAlerts
    let cameras: [HubCamera]
}

/// A recent person alert at a camera (from Frigate), newest first.
struct Visitor: Codable, Identifiable, Equatable {
    let id: String
    let start: Double
    let end: Double?
    let thumbnail: String?

    var date: Date { Date(timeIntervalSince1970: start) }
}

/// A one-time code from the hub for pairing another device (the iPhone asks for one for its watch).
struct PairingCode: Codable {
    let secret: String
    let code: String
    let expires: Double
}

enum HubError: LocalizedError {
    case notPaired, unauthorized, unreachable, status(Int)

    var errorDescription: String? {
        switch self {
        case .notPaired: return "This device isn't paired with a hub."
        case .unauthorized: return "The hub no longer accepts this device. Pair it again."
        case .unreachable: return "Can't reach the hub."
        case .status(let code): return "The hub answered \(code)."
        }
    }
}

/// Which way to the hub: straight to it on the home network, or through its away address.
/// Shared by every HubClient, so a switch applies to the next request and stream everywhere.
enum HubRoute {
    enum Way { case home, away }
    private static let lock = NSLock()
    nonisolated(unsafe) private static var _current = Way.home

    static var current: Way {
        get { lock.withLock { _current } }
        set { lock.withLock { _current = newValue } }
    }

    static var isHome: Bool { current == .home }
    static func reset() { current = .home }
}

/// Talks to the paired hub. API calls go over HTTPS to the hub's pinned certificate at home, or
/// to its away address, each signed with this device's key. Players get URLs with a short-lived
/// media token instead (plain HTTP at home, HTTPS away).
struct HubClient {
    var link: HubLink? { HubSession.shared.link }

    private static let pinned = NSLock()
    nonisolated(unsafe) private static var sessions: [String: URLSession] = [:]

    private func session(_ way: HubRoute.Way, _ link: HubLink) -> URLSession {
        guard way == .home else { return .shared }
        return Self.pinned.withLock {
            if let s = Self.sessions[link.fingerprint] { return s }
            let s = PinnedTrust.session(link.fingerprint).0
            Self.sessions[link.fingerprint] = s
            return s
        }
    }

    /// Current way first, then the other (if the hub has an away address).
    private func ways(_ link: HubLink) -> [HubRoute.Way] {
        let order: [HubRoute.Way] = HubRoute.current == .home ? [.home, .away] : [.away, .home]
        return order.filter { $0 == .home || link.away != nil }
    }

    /// A signed request to the hub, trying the current way first. The home address gets a short
    /// timeout, since away from home it never answers.
    func send(_ method: String, _ path: String, query: [URLQueryItem] = [], json: [String: Any]? = nil,
              timeout: TimeInterval = 10) async throws -> Data {
        guard let link, let key = HubSession.shared.key else { throw HubError.notPaired }
        var lastError: Error = HubError.unreachable
        for way in ways(link) {
            guard let base = way == .home ? link.homeAPI : link.away else { continue }
            var c = URLComponents(url: URL(string: path, relativeTo: base)!.absoluteURL, resolvingAgainstBaseURL: false)!
            let q = (c.queryItems ?? []) + query
            c.queryItems = q.isEmpty ? nil : q
            var req = URLRequest(url: c.url!, timeoutInterval: way == .home ? min(timeout, 3) : timeout)
            req.httpMethod = method
            if let json {
                req.httpBody = try JSONSerialization.data(withJSONObject: json)
                req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            }
            try RequestSigner.sign(&req, link: link, key: key)
            do {
                let (data, resp) = try await session(way, link).data(for: req)
                let status = (resp as? HTTPURLResponse)?.statusCode ?? 0
                if status == 401 { throw HubError.unauthorized }
                guard status == 200 else { throw HubError.status(status) }
                if way != HubRoute.current { HubRoute.current = way }
                return data
            } catch HubError.unauthorized {
                throw HubError.unauthorized          // the hub answered and refused: don't try elsewhere
            } catch {
                lastError = error
            }
        }
        throw lastError
    }

    func get(_ path: String, _ query: [URLQueryItem] = [], timeout: TimeInterval = 10) async throws -> Data {
        try await send("GET", path, query: query, timeout: timeout)
    }

    /// A URL a player can open by itself: carries a media token, valid for a few minutes.
    func mediaURL(_ path: String, _ query: [URLQueryItem] = []) async throws -> URL {
        guard let link else { throw HubError.notPaired }
        let token = try await MediaTokens.shared.current(self)
        let base = HubRoute.current == .home ? link.homeMedia : (link.away ?? link.homeMedia)
        var c = URLComponents(url: URL(string: path, relativeTo: base)!.absoluteURL, resolvingAgainstBaseURL: false)!
        c.queryItems = (c.queryItems ?? []) + query + [URLQueryItem(name: "token", value: token)]
        return c.url!
    }

    func cameras() async throws -> CameraList {
        try JSONDecoder().decode(CameraList.self, from: try await get("/api/watch/cameras"))
    }

    /// Flip the hub's shared alerts switch (all cameras when `camera` is nil). The desktop app and
    /// the projector follow the same switch.
    func setAlerts(_ enabled: Bool, camera: String? = nil) async throws -> HubAlerts {
        var body: [String: Any] = ["enabled": enabled]
        if let camera { body["camera"] = camera }
        return try JSONDecoder().decode(HubAlerts.self,
                                        from: try await send("POST", "/api/watch/alerts", json: body, timeout: 8))
    }

    func frame(_ cam: HubCamera, width: Int) async throws -> Data {
        // Int64, not Int: on 32-bit watches (arm64_32, e.g. the original Ultra) Int is 32 bits
        // and a millisecond timestamp overflows it, which traps
        let stamp = String(Int64(Date().timeIntervalSince1970 * 1000))  // defeat any cache
        return try await get(cam.frame, [URLQueryItem(name: "w", value: String(width)),
                                         URLQueryItem(name: "t", value: stamp)], timeout: 5)
    }

    /// Give the hub this app's push token so person alerts reach it even when it's closed.
    func registerPush(token: Data) async throws {
        #if DEBUG
        let env = "sandbox"          // Xcode installs talk to the APNs sandbox
        #else
        let env = "production"       // TestFlight / App Store
        #endif
        _ = try await send("POST", "/api/watch/devices", json: [
            "token": token.map { String(format: "%02x", $0) }.joined(),
            "topic": Bundle.main.bundleIdentifier ?? "", "env": env])
    }

    /// An image from the hub by path (e.g. a notification's detection snapshot).
    func image(_ path: String, width: Int) async throws -> Data {
        try await get(path, [URLQueryItem(name: "w", value: String(width))], timeout: 8)
    }

    func visitors(_ cam: HubCamera, limit: Int = 12) async throws -> [Visitor] {
        struct Reply: Decodable { let visitors: [Visitor] }
        let data = try await get("/api/watch/cameras/\(cam.id)/visitors",
                                 [URLQueryItem(name: "limit", value: String(limit))])
        return try JSONDecoder().decode(Reply.self, from: data).visitors
    }

    /// A one-time code for pairing another of this person's devices (their watch).
    func pairingCode() async throws -> PairingCode {
        try JSONDecoder().decode(PairingCode.self, from: try await send("POST", "/api/watch/pairing-code"))
    }

    /// Tell the hub to forget this device (its key stops working at once).
    func unpair() async throws {
        _ = try await send("POST", "/api/watch/unpair", timeout: 6)
    }

    func hlsURL(_ cam: HubCamera, variant: String? = nil) async -> URL? {
        guard let hls = cam.hls else { return nil }
        return try? await mediaURL(hls, variant.map { [URLQueryItem(name: "v", value: $0)] } ?? [])
    }

    func mjpegURL(_ cam: HubCamera, width: Int, fps: Int) async -> URL? {
        try? await mediaURL(cam.mjpeg ?? "/api/watch/cameras/\(cam.id)/live.mjpeg",
                            [URLQueryItem(name: "w", value: String(width)), URLQueryItem(name: "fps", value: String(fps))])
    }
}

/// Polls the hub for cameras and person state while the app is on screen.
@MainActor
final class CameraStore: ObservableObject {
    @Published var cameras: [HubCamera] = []
    @Published var alerts = HubAlerts(enabled: true, cameras: [:])
    @Published var unreachable = false
    /// The hub refused this device's key: it was removed on the setup page
    @Published var unauthorized = false
    let client = HubClient()
    private var task: Task<Void, Never>?

    func start() {
        guard task == nil else { return }
        task = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(nanoseconds: 3_000_000_000)
            }
        }
    }

    func stop() {
        task?.cancel()
        task = nil
    }

    /// Optimistic: the switch moves at once, and snaps back if the hub doesn't take it.
    func setAlerts(_ enabled: Bool, camera: String? = nil) {
        let before = alerts
        if let camera { alerts.cameras[camera] = enabled } else { alerts.enabled = enabled }
        Task {
            do { alerts = try await client.setAlerts(enabled, camera: camera) } catch { alerts = before }
        }
    }

    func refresh() async {
        do {
            let list = try await client.cameras()
            if list.cameras != cameras { cameras = list.cameras }
            if list.alerts != alerts { alerts = list.alerts }
            unreachable = false
            unauthorized = false
        } catch HubError.unauthorized {
            unauthorized = true
        } catch {
            unreachable = true
        }
    }
}
