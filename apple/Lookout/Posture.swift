import SwiftUI

// How the iPhone Duo is held. Adapted from Gatehouse's Posture.swift (copied, not shared): the fold
// region says where the crease is (iOS 27.1 `reservedRegions(kind: .division)`), the hinge says
// closed / half open / flat (`onHingeChange`).

/// Where the Duo's fold falls in a view. Present (inactive) when the Duo lies flat, active when
/// it's half open; nil on the cover screen and other iPhones.
struct FoldInfo: Equatable {
    /// The fold's band, including the system's margins for interactive content
    var frame: CGRect
    /// Half open: a real crease, nothing should sit across it
    var isActive: Bool

    /// The fold runs top to bottom (book), or side to side (laptop, or upright and flat)
    var isVertical: Bool { frame.height >= frame.width }
}

enum HingeStatus: Equatable {
    case closed, partiallyOpen, fullyOpen
}

/// Which home screen the app shows.
enum HomePosture: String, Equatable {
    /// Any other iPhone, and the Duo's cover screen: the regular layouts
    case phone
    /// The Duo open flat, upright: the big camera ends at the fold, the strip and Today below it
    case open
    /// Half open with the fold side to side: live camera on top, a timeline deck below
    case laptop
    /// Half open like a book (or flat and sideways): live camera on the left page, cameras and
    /// events on the right
    case book

    /// Inner-screen layouts with one big live camera
    func usesDuoStream(_ layout: HomeLayout) -> Bool {
        self == .laptop || self == .book || (self == .open && layout == .focus)
    }

    static func resolve(size: CGSize, hinge: HingeStatus?, fold: FoldInfo?) -> HomePosture {
        #if DEBUG
        if let forced = Self.debug { return forced }
        #endif
        guard let hinge, hinge != .closed, let fold else { return .phone }
        if size.width > size.height { return .book }            // book fold, or flat and sideways
        if hinge == .partiallyOpen || fold.isActive { return fold.isVertical ? .book : .laptop }
        return .open
    }

    #if DEBUG
    /// -LookoutPosture cover|open|laptop|book: that layout, with a fake fold through the middle
    static let debug: HomePosture? = {
        let args = ProcessInfo.processInfo.arguments
        guard let i = args.firstIndex(of: "-LookoutPosture"), i + 1 < args.count else { return nil }
        return args[i + 1] == "cover" ? .phone : HomePosture(rawValue: args[i + 1])
    }()

    /// The inner screen's size for that posture (the simulator can only screenshot the cover)
    static var debugScreen: CGSize? {
        switch debug {
        case .open, .laptop: return CGSize(width: 669, height: 951)
        case .book: return CGSize(width: 951, height: 669)
        default: return nil
        }
    }
    #endif
}

extension GeometryProxy {
    /// The Duo's fold in this view's coordinates.
    func fold() -> FoldInfo? {
        #if DEBUG
        if let posture = HomePosture.debug, posture != .phone {
            // Through the middle of the screen, like the real hinge
            let midX = (size.width + safeAreaInsets.leading + safeAreaInsets.trailing) / 2 - safeAreaInsets.leading
            let midY = (size.height + safeAreaInsets.top + safeAreaInsets.bottom) / 2 - safeAreaInsets.top
            return posture == .book
                ? FoldInfo(frame: CGRect(x: midX - 12, y: 0, width: 24, height: size.height), isActive: true)
                : FoldInfo(frame: CGRect(x: 0, y: midY - 12, width: size.width, height: 24), isActive: posture == .laptop)
        }
        #endif
        if #available(iOS 27.1, *) {
            let regions = reservedRegions(kind: .division, options: [.includeInactive])
            guard let region = regions.first(where: \.isActive) ?? regions.first else { return nil }
            return FoldInfo(frame: region.frame, isActive: region.isActive)
        }
        return nil
    }
}

/// Feeds hinge status changes (iOS 27.1 `onHingeChange`) into a binding; nil on iPhones without one.
private struct HingeObserver: ViewModifier {
    @Binding var status: HingeStatus?

    func body(content: Content) -> some View {
        if #available(iOS 27.1, *) {
            content.onHingeChange { _, context in
                guard let hinge = context.hinge else {
                    if status != nil { status = nil }
                    return
                }
                let next: HingeStatus = switch hinge.status {
                case .closed: .closed
                case .partiallyOpen: .partiallyOpen
                default: .fullyOpen          // Apple: treat unknown as fully open
                }
                if next != status { status = next }
            }
        } else {
            content
        }
    }
}

extension View {
    func observingHinge(_ status: Binding<HingeStatus?>) -> some View { modifier(HingeObserver(status: status)) }

    /// DEBUG: draw the app at the Duo's inner-screen size, scaled to fit, for `-LookoutPosture`.
    @ViewBuilder func debugScreen() -> some View {
        #if DEBUG
        if let screen = HomePosture.debugScreen {
            GeometryReader { geo in
                let full = CGSize(width: geo.size.width + geo.safeAreaInsets.leading + geo.safeAreaInsets.trailing,
                                  height: geo.size.height + geo.safeAreaInsets.top + geo.safeAreaInsets.bottom)
                let scale = min(full.width / screen.width, full.height / screen.height)
                self
                    .frame(width: screen.width, height: screen.height)
                    .scaleEffect(scale)
                    .frame(width: full.width, height: full.height)
            }
            .ignoresSafeArea()
            .background(Color(white: 0.2))
        } else {
            self
        }
        #else
        self
        #endif
    }
}
