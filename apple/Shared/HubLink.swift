import CryptoKit
import Foundation
import Security

/// The hub this device is paired with, as learned when pairing. Kept in the keychain.
struct HubLink: Codable, Equatable {
    var hubID: String
    var name: String
    var address: String          // the hub on the home network (IP or host name)
    var httpsPort: Int           // app API, TLS with the hub's own certificate
    var httpPort: Int            // video and images, opened by URL with a media token
    var fingerprint: String      // SHA-256 (hex) of the hub's certificate, pinned
    var remoteURL: String?       // away from home, e.g. https://lookout.example.com
    var deviceID: String

    var homeAPI: URL { URL(string: "https://\(address):\(httpsPort)")! }
    var homeMedia: URL { URL(string: "http://\(address):\(httpPort)")! }
    var away: URL? { remoteURL.flatMap { URL(string: $0) } }
}

/// Generic-password items in the keychain. On the watch, the app and its complication share them
/// through the access group named in Info.plist (LookoutKeychainGroup).
enum Keychain {
    private static let service = "dev.lookout.hub"
    private static let group = Bundle.main.object(forInfoDictionaryKey: "LookoutKeychainGroup") as? String

    private static func query(_ account: String, group: String?) -> [String: Any] {
        var q: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                kSecAttrService as String: service,
                                kSecAttrAccount as String: account]
        if let group, !group.isEmpty, !group.hasPrefix("$(") { q[kSecAttrAccessGroup as String] = group }
        return q
    }

    static func get(_ account: String) -> Data? {
        for g in [group, nil] {
            var q = query(account, group: g)
            q[kSecReturnData as String] = true
            q[kSecMatchLimit as String] = kSecMatchLimitOne
            var out: CFTypeRef?
            if SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let data = out as? Data { return data }
        }
        return nil
    }

    static func set(_ data: Data, _ account: String) {
        delete(account)
        for g in [group, nil] {
            var q = query(account, group: g)
            q[kSecValueData as String] = data
            // readable in the background after the first unlock (alerts, complication), never
            // backed up or moved to another device
            q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            if SecItemAdd(q as CFDictionary, nil) == errSecSuccess { return }
        }
    }

    static func delete(_ account: String) {
        for g in [group, nil] { SecItemDelete(query(account, group: g) as CFDictionary) }
    }
}

/// This device's signing key. In the Secure Enclave where there is one (it can't be copied off
/// the device); a software key in the keychain otherwise (simulators).
enum DeviceKey {
    case enclave(SecureEnclave.P256.Signing.PrivateKey)
    case software(P256.Signing.PrivateKey)

    private static let account = "device-key"

    static func load() -> DeviceKey? {
        guard let data = Keychain.get(account), let kind = data.first else { return nil }
        let body = data.dropFirst()
        if kind == UInt8(ascii: "E"), let k = try? SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: body) {
            return .enclave(k)
        }
        if kind == UInt8(ascii: "S"), let k = try? P256.Signing.PrivateKey(rawRepresentation: body) {
            return .software(k)
        }
        return nil
    }

    /// A fresh key, not yet stored: `stored` is what `HubSession.save(_:key:stored:)` keeps once
    /// pairing has worked, so a failed attempt leaves the old pairing as it was.
    static func generate() throws -> (key: DeviceKey, stored: Data) {
        let key: DeviceKey
        var stored = Data()
        if SecureEnclave.isAvailable {
            var error: Unmanaged<CFError>?
            guard let access = SecAccessControlCreateWithFlags(nil, kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
                                                               .privateKeyUsage, &error) else {
                throw error!.takeRetainedValue() as Error
            }
            let k = try SecureEnclave.P256.Signing.PrivateKey(accessControl: access)
            key = .enclave(k)
            stored = Data("E".utf8) + k.dataRepresentation
        } else {
            let k = P256.Signing.PrivateKey()
            key = .software(k)
            stored = Data("S".utf8) + k.rawRepresentation
        }
        return (key, stored)
    }

    static func store(_ stored: Data) { Keychain.set(stored, account) }
    static func delete() { Keychain.delete(account) }

    /// Uncompressed X9.63 point, as the hub expects
    var publicKey: Data {
        switch self {
        case .enclave(let k): return k.publicKey.x963Representation
        case .software(let k): return k.publicKey.x963Representation
        }
    }

    func sign(_ message: Data) throws -> Data {
        switch self {
        case .enclave(let k): return try k.signature(for: message).derRepresentation
        case .software(let k): return try k.signature(for: message).derRepresentation
        }
    }
}

/// The pairing in use, shared by every HubClient in the process. Posts `.hubLinkChanged`.
final class HubSession: @unchecked Sendable {
    static let shared = HubSession()
    private let lock = NSLock()
    private var _link: HubLink?
    private var _key: DeviceKey?
    private var loaded = false
    private static let account = "hub-link"

    var link: HubLink? { lock.withLock { loadIfNeeded(); return _link } }
    var key: DeviceKey? { lock.withLock { loadIfNeeded(); return _key } }

    private func loadIfNeeded() {
        guard !loaded else { return }
        loaded = true
        _link = Keychain.get(Self.account).flatMap { try? JSONDecoder().decode(HubLink.self, from: $0) }
        _key = DeviceKey.load()
        if _key == nil { _link = nil }
    }

    func save(_ link: HubLink, key: (key: DeviceKey, stored: Data)? = nil) {
        lock.withLock {
            loadIfNeeded()
            _link = link
            if let key {
                DeviceKey.store(key.stored)
                _key = key.key
            }
            if let data = try? JSONEncoder().encode(link) { Keychain.set(data, Self.account) }
        }
        NotificationCenter.default.post(name: .hubLinkChanged, object: nil)
    }

    func clear() {
        lock.withLock {
            _link = nil
            _key = nil
            loaded = true
            Keychain.delete(Self.account)
            DeviceKey.delete()
        }
        HubRoute.reset()
        MediaTokens.shared.clear()
        NotificationCenter.default.post(name: .hubLinkChanged, object: nil)
    }

    /// Reload after another process (the watch app vs. its complication) changed the keychain.
    func reload() { lock.withLock { loaded = false } }
}

extension Notification.Name {
    static let hubLinkChanged = Notification.Name("LookoutHubLinkChanged")
}

/// For SwiftUI: whether this device is paired, and with which hub.
@MainActor
final class LinkStore: ObservableObject {
    static let shared = LinkStore()
    @Published private(set) var link: HubLink? = HubSession.shared.link
    private var observer: NSObjectProtocol?

    private init() {
        observer = NotificationCenter.default.addObserver(forName: .hubLinkChanged, object: nil, queue: .main) { _ in
            Task { @MainActor in LinkStore.shared.link = HubSession.shared.link }
        }
    }
}

/// Signs requests: `Authorization: Lookout <device>:<unix time>:<base64 DER signature>` over
/// "<METHOD>\n<path?query>\n<unix time>", exactly as sent.
enum RequestSigner {
    static func sign(_ request: inout URLRequest, link: HubLink, key: DeviceKey) throws {
        guard let url = request.url else { return }
        let ts = String(Int64(Date().timeIntervalSince1970))     // Int64: 32-bit watches
        var target = url.path(percentEncoded: true)
        if target.isEmpty { target = "/" }
        if let q = url.query(percentEncoded: true), !q.isEmpty { target += "?" + q }
        let message = "\(request.httpMethod ?? "GET")\n\(target)\n\(ts)"
        let sig = try key.sign(Data(message.utf8)).base64EncodedString()
        request.setValue("Lookout \(link.deviceID):\(ts):\(sig)", forHTTPHeaderField: "Authorization")
    }
}

/// HTTPS to the hub's own certificate: accepted only when its SHA-256 matches the pinned
/// fingerprint (or, while pairing from a Bonjour listing, starts with the advertised prefix).
/// Records the certificate it saw so pairing can pin the full fingerprint.
final class PinnedTrust: NSObject, URLSessionDelegate, @unchecked Sendable {
    let expected: String
    private(set) var seen: String?

    init(expected: String) { self.expected = expected.lowercased() }

    static func session(_ expected: String, timeout: TimeInterval = 10) -> (URLSession, PinnedTrust) {
        let trust = PinnedTrust(expected: expected)
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = timeout
        config.waitsForConnectivity = false
        return (URLSession(configuration: config, delegate: trust, delegateQueue: nil), trust)
    }

    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust,
              let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate], let leaf = chain.first else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        let der = SecCertificateCopyData(leaf) as Data
        let fp = SHA256.hash(data: der).map { String(format: "%02x", $0) }.joined()
        seen = fp
        if !expected.isEmpty, fp.hasPrefix(expected) {
            completionHandler(.useCredential, URLCredential(trust: trust))
        } else {
            completionHandler(.cancelAuthenticationChallenge, nil)
        }
    }
}

/// Short-lived tokens for things opened by URL (video players, the MJPEG stream).
final class MediaTokens: @unchecked Sendable {
    static let shared = MediaTokens()
    private let lock = NSLock()
    private var token: String?
    private var expires: Double = 0

    func clear() { lock.withLock { token = nil; expires = 0 } }

    func current(_ client: HubClient) async throws -> String {
        if let t = lock.withLock({ Date().timeIntervalSince1970 < expires - 60 ? token : nil }) { return t }
        struct Reply: Decodable { let token: String; let expires: Double }
        let reply = try JSONDecoder().decode(Reply.self, from: try await client.get("/api/watch/media-token"))
        lock.withLock { token = reply.token; expires = reply.expires }
        return reply.token
    }
}
