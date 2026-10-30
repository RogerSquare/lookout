import Foundation

/// How to reach a hub and the one-time code to pair with it: from the setup page's QR code
/// (lookout://pair?...), from a hub found on the network plus the 6-digit code, or handed from
/// the iPhone to its watch.
struct PairingInvite: Equatable {
    var hubID: String?
    var name: String
    var address: String
    var httpsPort: Int
    var httpPort: Int
    var fingerprint: String      // full from a QR code; the advertised prefix for a found hub
    var remoteURL: String?
    var code: String

    /// lookout://pair?h=<hub id>&n=<name>&a=<address>&p=<https port>&q=<http port>&fp=<sha256>&c=<code>[&r=<away url>]
    init?(url: URL) {
        guard url.scheme == "lookout", url.host() == "pair",
              let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems else { return nil }
        let q = Dictionary(items.map { ($0.name, $0.value ?? "") }, uniquingKeysWith: { a, _ in a })
        guard let address = q["a"], !address.isEmpty, let fp = q["fp"], fp.count == 64,
              let code = q["c"], !code.isEmpty else { return nil }
        self.init(hubID: q["h"], name: q["n"] ?? "Lookout Hub", address: address,
                  httpsPort: Int(q["p"] ?? "") ?? 8766, httpPort: Int(q["q"] ?? "") ?? 8765,
                  fingerprint: fp, remoteURL: q["r"].flatMap { $0.isEmpty ? nil : $0 }, code: code)
    }

    init(hubID: String?, name: String, address: String, httpsPort: Int, httpPort: Int, fingerprint: String,
         remoteURL: String?, code: String) {
        self.hubID = hubID
        self.name = name
        self.address = address
        self.httpsPort = httpsPort
        self.httpPort = httpPort
        self.fingerprint = fingerprint.lowercased()
        self.remoteURL = remoteURL
        self.code = code
    }

    var url: URL {
        var c = URLComponents()
        c.scheme = "lookout"
        c.host = "pair"
        c.queryItems = [URLQueryItem(name: "h", value: hubID), URLQueryItem(name: "n", value: name),
                        URLQueryItem(name: "a", value: address), URLQueryItem(name: "p", value: String(httpsPort)),
                        URLQueryItem(name: "q", value: String(httpPort)), URLQueryItem(name: "fp", value: fingerprint),
                        URLQueryItem(name: "c", value: code)] + (remoteURL.map { [URLQueryItem(name: "r", value: $0)] } ?? [])
        return c.url!
    }
}

enum PairingError: LocalizedError {
    case refused(String), wrongHub, unreachable

    var errorDescription: String? {
        switch self {
        case .refused(let why): return why
        case .wrongHub: return "That hub's certificate doesn't match the code. Scan the code again from the hub's setup page."
        case .unreachable: return "Can't reach the hub. Make sure this device is on the same network as the hub, then try again."
        }
    }
}

enum Pairing {
    private struct Reply: Decodable {
        struct Hub: Decodable {
            let hub_id: String
            let name: String
            let fingerprint: String
            let https_port: Int
            let http_port: Int
            let address: String
            let remote_url: String?
        }
        let device_id: String
        let hub: Hub
    }

    /// Make this device's key, register it with the hub and remember the hub. Tries the hub on the
    /// home network first (its certificate checked against the invite), then its away address.
    @discardableResult
    static func pair(_ invite: PairingInvite, deviceName: String, platform: String) async throws -> HubLink {
        let key = try DeviceKey.generate()
        let body = try JSONSerialization.data(withJSONObject: [
            "code": invite.code, "name": deviceName, "platform": platform,
            "public_key": key.key.publicKey.base64EncodedString()])

        var lastError: Error = PairingError.unreachable
        // 1. Home network, pinned to the invite's certificate
        do {
            let (session, trust) = PinnedTrust.session(invite.fingerprint, timeout: 6)
            defer { session.finishTasksAndInvalidate() }
            let reply = try await post(session, URL(string: "https://\(invite.address):\(invite.httpsPort)/api/watch/pair")!, body)
            guard let seen = trust.seen, reply.hub.fingerprint.lowercased() == seen else { throw PairingError.wrongHub }
            return save(reply, key: key)
        } catch let e as PairingError {
            if case .refused = e { throw e }
            if case .wrongHub = e { throw e }
            lastError = e
        } catch {
            lastError = PairingError.unreachable
        }
        // 2. Away address (normal web certificate). Only full codes work from outside the home
        // network, and the hub must report the certificate the invite named.
        if let remote = invite.remoteURL, let base = URL(string: remote), invite.fingerprint.count == 64 {
            do {
                let reply = try await post(.shared, base.appending(path: "api/watch/pair"), body)
                guard reply.hub.fingerprint.lowercased() == invite.fingerprint else { throw PairingError.wrongHub }
                return save(reply, key: key)
            } catch {
                lastError = error
            }
        }
        throw lastError
    }

    private static func post(_ session: URLSession, _ url: URL, _ body: Data) async throws -> Reply {
        var req = URLRequest(url: url, timeoutInterval: 10)
        req.httpMethod = "POST"
        req.httpBody = body
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let (data, resp) = try await session.data(for: req)
        let status = (resp as? HTTPURLResponse)?.statusCode ?? 0
        if status == 403 || status == 400 {
            throw PairingError.refused(String(data: data, encoding: .utf8)?
                .replacingOccurrences(of: "\(status): ", with: "") ?? "The hub refused the code.")
        }
        guard status == 200 else { throw PairingError.unreachable }
        return try JSONDecoder().decode(Reply.self, from: data)
    }

    private static func save(_ reply: Reply, key: (key: DeviceKey, stored: Data)) -> HubLink {
        let h = reply.hub
        let link = HubLink(hubID: h.hub_id, name: h.name, address: h.address, httpsPort: h.https_port,
                           httpPort: h.http_port, fingerprint: h.fingerprint.lowercased(), remoteURL: h.remote_url,
                           deviceID: reply.device_id)
        HubRoute.reset()
        MediaTokens.shared.clear()
        HubSession.shared.save(link, key: key)
        return link
    }

    /// An invite for another of this person's devices (the watch), from the paired hub.
    static func companionInvite() async throws -> PairingInvite {
        guard let link = HubSession.shared.link else { throw HubError.notPaired }
        let code = try await HubClient().pairingCode()
        return PairingInvite(hubID: link.hubID, name: link.name, address: link.address, httpsPort: link.httpsPort,
                             httpPort: link.httpPort, fingerprint: link.fingerprint, remoteURL: link.remoteURL,
                             code: code.secret)
    }

    /// Forget the hub here, and tell the hub to forget this device if it can be reached.
    static func unpair() async {
        try? await HubClient().unpair()
        HubSession.shared.clear()
    }
}
