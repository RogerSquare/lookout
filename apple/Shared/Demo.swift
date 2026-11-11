#if DEBUG && os(iOS)
import UIKit

/// Fake cameras for simulator screenshots and layout checks, no hub needed:
/// launch with `-LookoutDemoCameras <n>` (1-6). Debug builds only.
enum Demo {
    static let count: Int? = {
        let args = ProcessInfo.processInfo.arguments
        guard let i = args.lastIndex(of: "-LookoutDemoCameras"), i + 1 < args.count,
              let n = Int(args[i + 1]), n > 0 else { return nil }
        return min(n, names.count)
    }()

    static var enabled: Bool { count != nil }

    private static let names = ["Front Door", "Bedroom", "Garage", "Back Yard", "Driveway", "Nursery"]
    private static let tints: [UIColor] = [
        UIColor(red: 0.12, green: 0.19, blue: 0.31, alpha: 1), UIColor(red: 0.20, green: 0.16, blue: 0.13, alpha: 1),
        UIColor(red: 0.16, green: 0.18, blue: 0.16, alpha: 1), UIColor(red: 0.10, green: 0.20, blue: 0.14, alpha: 1),
        UIColor(red: 0.18, green: 0.18, blue: 0.22, alpha: 1), UIColor(red: 0.22, green: 0.16, blue: 0.20, alpha: 1),
    ]

    /// Front Door has someone at it; the 4th camera is offline.
    static func cameras(_ n: Int) -> [HubCamera] {
        (0..<n).map { i in
            HubCamera(id: "demo\(i)", name: names[i], connected: i != 3, person: i == 0,
                      since: nil, lastSeen: nil, alerts: true, frame: "", hls: nil, mjpeg: nil, ptz: i == 1,
                      hlsVariants: ["phone", "hd"],
                      caps: i == 1 ? ["sleep", "night_vision", "status_light", "ptz", "presets"]
                                   : ["sleep", "night_vision", "status_light"],
                      asleep: i == 2, onDemand: i == 4)
        }
    }

    /// Today's visitors at the demo Front Door (and one at Garage), newest first
    static func visitors(_ cam: HubCamera) -> [Visitor] {
        let now = Date().timeIntervalSince1970
        let ago: [(Double, Double)] = cam.id == "demo0" ? [(25, 42), (190, 18), (450, 64)]
            : cam.id == "demo2" ? [(300, 30)] : []
        return ago.map { minutes, length in
            let start = now - minutes * 60
            return Visitor(id: "\(cam.id)-\(Int(minutes))", start: start, end: start + length, thumbnail: nil)
        }
    }

    static func isDemo(_ cam: HubCamera) -> Bool { enabled && cam.id.hasPrefix("demo") }

    /// A plain 16:9 stand-in picture: a tinted room with the camera's name.
    static func image(_ cam: HubCamera) -> UIImage {
        let i = Int(cam.id.dropFirst(4)) ?? 0
        let size = CGSize(width: 1280, height: 720)
        return UIGraphicsImageRenderer(size: size).image { ctx in
            let c = ctx.cgContext
            tints[i % tints.count].setFill()
            c.fill(CGRect(origin: .zero, size: size))
            UIColor(white: 0, alpha: 0.35).setFill()
            c.fill(CGRect(x: 0, y: 470, width: 1280, height: 250))           // floor
            UIColor(white: 1, alpha: 0.10).setFill()
            c.fill(CGRect(x: 120 + CGFloat(i) * 60, y: 120, width: 260, height: 220))   // window
            UIColor(white: 1, alpha: 0.06).setFill()
            c.fill(CGRect(x: 820, y: 90, width: 220, height: 420))           // door
            let text = cam.name as NSString
            text.draw(at: CGPoint(x: 60, y: 600), withAttributes: [
                .font: UIFont.systemFont(ofSize: 44, weight: .semibold),
                .foregroundColor: UIColor(white: 1, alpha: 0.35),
            ])
        }
    }
}
#endif
