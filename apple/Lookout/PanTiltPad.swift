import SwiftUI

/// Arrows on glass for cameras that can turn. Each tap nudges the camera about 30 degrees; taps
/// while it's still moving are dropped by the hub, so mashing a button can't queue a spin.
struct PanTiltPad: View {
    let camera: HubCamera
    let client: HubClient
    @State private var moving = false
    @State private var failed = false

    var body: some View {
        VStack(spacing: 4) {
            arrow("chevron.up", "Tilt up", pan: 0, tilt: 1)
            HStack(spacing: 4) {
                arrow("chevron.left", "Turn left", pan: -1, tilt: 0)
                ZStack {
                    if moving {
                        ProgressView().controlSize(.small)
                    } else {
                        Image(systemName: failed ? "exclamationmark.triangle.fill" : "video.fill")
                            .font(.caption)
                            .foregroundStyle(failed ? AnyShapeStyle(.yellow) : AnyShapeStyle(.secondary))
                    }
                }
                .frame(width: 44, height: 44)
                arrow("chevron.right", "Turn right", pan: 1, tilt: 0)
            }
            arrow("chevron.down", "Tilt down", pan: 0, tilt: -1)
        }
        .padding(8)
        .glassEffect(.regular, in: .circle)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Pan and tilt")
    }

    private func arrow(_ symbol: String, _ label: String, pan: Double, tilt: Double) -> some View {
        Button {
            Task { await move(pan: pan, tilt: tilt) }
        } label: {
            Image(systemName: symbol)
                .font(.system(size: 18, weight: .semibold))
                .frame(width: 44, height: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }

    private func move(pan: Double, tilt: Double) async {
        guard !moving else { return }
        moving = true
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
        do {
            try await client.ptz(camera, pan: pan, tilt: tilt)
            failed = false
        } catch HubError.status(409) {
            // still moving from the last tap
        } catch {
            failed = true
            UINotificationFeedbackGenerator().notificationOccurred(.error)
        }
        moving = false
    }
}
