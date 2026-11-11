import SwiftUI

/// The last few times someone was at this camera, as snapshots (from Frigate). Hidden when the
/// hub has none to offer (its own detector doesn't keep a history).
struct VisitorsStrip: View {
    let camera: HubCamera
    let client: HubClient
    @State private var visitors: [Visitor] = []
    @State private var open: Visitor?

    var body: some View {
        Group {
            if !visitors.isEmpty {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Recent Visitors")
                        .font(.headline)
                        .padding(.horizontal, 6)
                    ScrollView(.horizontal, showsIndicators: false) {
                        LazyHStack(spacing: 12) {
                            ForEach(visitors) { v in
                                Button { open = v } label: { VisitorCard(visitor: v, client: client) }
                                    .buttonStyle(.plain)
                            }
                        }
                        .padding(.horizontal, 2)
                    }
                }
            }
        }
        .task(id: camera.id) {
            if let list = try? await client.visitors(camera) {
                withAnimation(.easeOut(duration: 0.2)) { visitors = list.filter { $0.thumbnail != nil } }
            }
        }
        .sheet(item: $open) { v in
            VisitorDetail(visitor: v, camera: camera, client: client)
                .presentationDetents([.medium, .large])
        }
    }
}

private struct VisitorCard: View {
    let visitor: Visitor
    let client: HubClient
    @State private var image: UIImage?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ZStack {
                Color(white: 0.15)
                if let image { Image(uiImage: image).resizable().scaledToFill() }
            }
            .frame(width: 132, height: 132)
            .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
            Text(visitor.date, format: .relative(presentation: .named))
                .font(.caption.weight(.medium))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 4)
        }
        .task(id: visitor.id) {
            if let path = visitor.thumbnail, let data = try? await client.get(path, [URLQueryItem(name: "h", value: "264")]) {
                image = UIImage(data: data)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Visitor \(visitor.date.formatted(.relative(presentation: .named)))")
    }
}

struct VisitorDetail: View {
    let visitor: Visitor
    let camera: HubCamera
    let client: HubClient
    @State private var image: UIImage?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            ZStack {
                Color(white: 0.12)
                if let image { Image(uiImage: image).resizable().scaledToFit() } else { ProgressView() }
            }
            .aspectRatio(16 / 9, contentMode: .fit)
            .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
            Text(camera.name).font(.title3.weight(.semibold))
            Text(visitor.date.formatted(date: .abbreviated, time: .shortened))
                .foregroundStyle(.secondary)
            Spacer()
        }
        .padding(20)
        .task {
            if let path = visitor.thumbnail, let data = try? await client.get(path, [URLQueryItem(name: "h", value: "720")]) {
                image = UIImage(data: data)
            }
        }
    }
}
