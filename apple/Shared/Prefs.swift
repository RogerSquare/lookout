import Foundation
import WatchConnectivity

/// What the watch shows, set from the iPhone app. Stored on both devices; the iPhone pushes it to
/// the watch as WatchConnectivity application context (delivered even if the watch app is closed,
/// latest value wins).
@MainActor
final class Prefs: NSObject, ObservableObject {
    static let shared = Prefs()

    /// Watch app: handles a pairing invite from the iPhone (set by WatchPairing; the complication,
    /// which shares this file, has none)
    static var onInvite: ((String) -> Void)?

    /// Camera ids hidden from the watch's pages
    @Published private(set) var hiddenOnWatch: Set<String>
    /// Camera ids in the order the user arranged them (cameras not listed go last, hub order)
    @Published private(set) var order: [String]
    /// Camera ids hidden from the iPhone's home screen (iPhone only; the watch has its own)
    @Published private(set) var hiddenOnPhone: Set<String>

    private let defaults = UserDefaults.standard
    private let hiddenKey = "hiddenOnWatch"
    private let orderKey = "cameraOrder"
    private let phoneHiddenKey = "hiddenOnPhone"

    private override init() {
        hiddenOnWatch = Set(UserDefaults.standard.stringArray(forKey: hiddenKey) ?? [])
        order = UserDefaults.standard.stringArray(forKey: orderKey) ?? []
        hiddenOnPhone = Set(UserDefaults.standard.stringArray(forKey: phoneHiddenKey) ?? [])
        super.init()
        if WCSession.isSupported() {
            WCSession.default.delegate = self
            WCSession.default.activate()
        }
    }

    func showsOnWatch(_ id: String) -> Bool { !hiddenOnWatch.contains(id) }

    func setShowsOnWatch(_ id: String, _ show: Bool) {
        var hidden = hiddenOnWatch
        if show { hidden.remove(id) } else { hidden.insert(id) }
        apply(hidden)
        push()
    }

    func showsOnPhone(_ id: String) -> Bool { !hiddenOnPhone.contains(id) }

    func setShowsOnPhone(_ id: String, _ show: Bool) {
        var hidden = hiddenOnPhone
        if show { hidden.remove(id) } else { hidden.insert(id) }
        guard hidden != hiddenOnPhone else { return }
        hiddenOnPhone = hidden
        defaults.set(Array(hidden).sorted(), forKey: phoneHiddenKey)
    }

    /// The cameras the iPhone's home screen shows: the user's order, hidden ones left out.
    func onPhone(_ cameras: [HubCamera]) -> [HubCamera] {
        sorted(cameras).filter { showsOnPhone($0.id) }
    }

    /// Cameras sorted by the user's order; ones never arranged keep the hub's order, after them.
    func sorted(_ cameras: [HubCamera]) -> [HubCamera] {
        let rank = Dictionary(uniqueKeysWithValues: order.enumerated().map { ($1, $0) })
        return cameras.enumerated()
            .sorted { (rank[$0.element.id] ?? order.count + $0.offset, $0.offset)
                    < (rank[$1.element.id] ?? order.count + $1.offset, $1.offset) }
            .map(\.element)
    }

    /// Reorder (from the iPhone's Settings list): `cameras` is the full list as currently shown.
    func move(_ cameras: [HubCamera], from source: IndexSet, to destination: Int) {
        var ids = sorted(cameras).map(\.id)
        ids.move(fromOffsets: source, toOffset: destination)
        applyOrder(ids)
        push()
    }

    private func apply(_ hidden: Set<String>) {
        guard hidden != hiddenOnWatch else { return }
        hiddenOnWatch = hidden
        defaults.set(Array(hidden).sorted(), forKey: hiddenKey)
    }

    private func applyOrder(_ ids: [String]) {
        guard ids != order else { return }
        order = ids
        defaults.set(ids, forKey: orderKey)
    }

    /// iPhone -> watch. Also carries the hub's current home address, so the watch follows it if
    /// the hub moves (the watch can't look for hubs on the network itself).
    func push() {
        #if os(iOS)
        let session = WCSession.default
        guard session.activationState == .activated, session.isPaired, session.isWatchAppInstalled else { return }
        var context: [String: Any] = [hiddenKey: Array(hiddenOnWatch).sorted(), orderKey: order]
        if let link = HubSession.shared.link {
            context["hubID"] = link.hubID
            context["hubAddress"] = link.address
            context["hubRemote"] = link.remoteURL ?? ""
        }
        try? session.updateApplicationContext(context)
        #endif
    }

    fileprivate func received(_ context: [String: Any]) {
        if let hidden = context[hiddenKey] as? [String] { apply(Set(hidden)) }
        if let ids = context[orderKey] as? [String] { applyOrder(ids) }
        if var link = HubSession.shared.link, link.hubID == context["hubID"] as? String,
           let address = context["hubAddress"] as? String, !address.isEmpty {
            let remote = (context["hubRemote"] as? String).flatMap { $0.isEmpty ? nil : $0 }
            if link.address != address || link.remoteURL != remote {
                link.address = address
                link.remoteURL = remote
                HubSession.shared.save(link)
            }
        }
    }

    #if os(iOS)
    /// Pair the watch: get a one-time code from the hub and hand it over. Queued by the system, so
    /// it arrives even if the watch app isn't running.
    func sendWatchInvite() async throws {
        let session = WCSession.default
        guard session.activationState == .activated, session.isPaired, session.isWatchAppInstalled else {
            throw WatchLinkError.noWatch
        }
        let invite = try await Pairing.companionInvite()
        session.transferUserInfo(["invite": invite.url.absoluteString])
    }

    var watchAvailable: Bool {
        let s = WCSession.default
        return s.activationState == .activated && s.isPaired && s.isWatchAppInstalled
    }
    #endif

    fileprivate func activated() {
        #if os(iOS)
        push()                       // make sure the watch has the latest after launch
        #else
        received(WCSession.default.receivedApplicationContext)
        #endif
    }
}

extension Prefs: WCSessionDelegate {
    nonisolated func session(_ session: WCSession, activationDidCompleteWith state: WCSessionActivationState,
                             error: Error?) {
        Task { @MainActor in Prefs.shared.activated() }
    }

    nonisolated func session(_ session: WCSession, didReceiveApplicationContext context: [String: Any]) {
        Task { @MainActor in Prefs.shared.received(context) }
    }

    #if os(iOS)
    /// The watch asks for an invite when it opens unpaired (this wakes the iPhone app if needed).
    nonisolated func session(_ session: WCSession, didReceiveMessage message: [String: Any],
                             replyHandler: @escaping ([String: Any]) -> Void) {
        guard message["needInvite"] != nil else { replyHandler([:]); return }
        Task {
            do {
                let invite = try await Pairing.companionInvite()
                replyHandler(["invite": invite.url.absoluteString])
            } catch {
                replyHandler(["error": error.localizedDescription])
            }
        }
    }
    #else
    nonisolated func session(_ session: WCSession, didReceiveUserInfo userInfo: [String: Any] = [:]) {
        if let invite = userInfo["invite"] as? String {
            Task { @MainActor in Prefs.onInvite?(invite) }
        }
    }
    #endif

    #if os(iOS)
    nonisolated func sessionDidBecomeInactive(_ session: WCSession) {}
    nonisolated func sessionDidDeactivate(_ session: WCSession) { session.activate() }
    nonisolated func sessionWatchStateDidChange(_ session: WCSession) {
        Task { @MainActor in Prefs.shared.push() }
    }
    #endif
}

enum WatchLinkError: LocalizedError {
    case noWatch
    var errorDescription: String? { "Install Lookout on your Apple Watch first (Watch app on iPhone, then Available Apps)." }
}
