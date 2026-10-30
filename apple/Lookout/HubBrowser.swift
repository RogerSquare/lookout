import Foundation
import Network

/// Lookout hubs announcing themselves on this network (Bonjour _lookout._tcp). Used when pairing,
/// and to follow the paired hub if its address changes: whenever it's seen here, the app talks to
/// it directly instead of through the away address.
@MainActor
final class HubBrowser: ObservableObject {
    struct Found: Identifiable, Equatable {
        let id: String           // hub id
        let name: String
        let address: String
        let httpsPort: Int
        let httpPort: Int
        let fingerprintPrefix: String
    }

    @Published private(set) var hubs: [Found] = []
    private var browser: NWBrowser?

    func start() {
        guard browser == nil else { return }
        let b = NWBrowser(for: .bonjourWithTXTRecord(type: "_lookout._tcp", domain: nil), using: .tcp)
        b.browseResultsChangedHandler = { results, _ in
            let found: [Found] = results.compactMap { r in
                guard case .bonjour(let txt) = r.metadata, let id = txt["id"], let a = txt["a"] else { return nil }
                return Found(id: id, name: txt["n"] ?? "Lookout Hub", address: a,
                             httpsPort: Int(txt["p"] ?? "") ?? 8766, httpPort: Int(txt["http"] ?? "") ?? 8765,
                             fingerprintPrefix: txt["fp"] ?? "")
            }
            Task { @MainActor in self.update(found) }
        }
        b.start(queue: .main)
        browser = b
    }

    func stop() {
        browser?.cancel()
        browser = nil
    }

    private func update(_ found: [Found]) {
        let unique = Dictionary(found.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a }).values
            .sorted { $0.name < $1.name }
        if unique != hubs { hubs = unique }
        // Our hub is right here: go direct, and follow it if its address changed
        if var link = HubSession.shared.link, let mine = unique.first(where: { $0.id == link.hubID }) {
            HubRoute.current = .home
            if link.address != mine.address || link.httpsPort != mine.httpsPort || link.httpPort != mine.httpPort {
                link.address = mine.address
                link.httpsPort = mine.httpsPort
                link.httpPort = mine.httpPort
                HubSession.shared.save(link)
                Prefs.shared.push()          // and tell the watch
            }
        }
    }

    func invite(_ hub: Found, code: String) -> PairingInvite {
        PairingInvite(hubID: hub.id, name: hub.name, address: hub.address, httpsPort: hub.httpsPort,
                      httpPort: hub.httpPort, fingerprint: hub.fingerprintPrefix, remoteURL: nil, code: code)
    }
}
