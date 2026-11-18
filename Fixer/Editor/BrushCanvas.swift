import SwiftUI
import UIKit

/// A brush stroke in IMAGE pixel coordinates.
struct Stroke: Equatable {
    var points: [CGPoint]
    var width: CGFloat
    var erase: Bool
}

enum BrushMask {
    /// Renders strokes into a grayscale mask the size of the photo (white = painted).
    static func render(_ strokes: [Stroke], size: CGSize) -> CGImage? {
        guard !strokes.isEmpty else { return nil }
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        format.opaque = true
        let img = UIGraphicsImageRenderer(size: size, format: format).image { ctx in
            let c = ctx.cgContext
            c.setFillColor(UIColor.black.cgColor)
            c.fill(CGRect(origin: .zero, size: size))
            c.setLineCap(.round)
            c.setLineJoin(.round)
            for s in strokes {
                c.setStrokeColor((s.erase ? UIColor.black : UIColor.white).cgColor)
                c.setFillColor((s.erase ? UIColor.black : UIColor.white).cgColor)
                c.setLineWidth(s.width)
                if s.points.count == 1, let p = s.points.first {
                    c.fillEllipse(in: CGRect(x: p.x - s.width / 2, y: p.y - s.width / 2, width: s.width, height: s.width))
                    continue
                }
                c.beginPath()
                c.addLines(between: s.points)
                c.strokePath()
            }
        }
        guard let cg = img.cgImage else { return nil }
        let w = cg.width, h = cg.height
        return ImageKit.grayImage(bytes: ImageKit.grayBytes(cg, width: w, height: h), width: w, height: h)
    }
}

/// Shows the photo aspect-fit, with the current mask tinted red, and lets the user paint the mask.
struct BrushCanvas: View {
    let image: UIImage
    let maskOverlay: UIImage?
    let painting: Bool
    let erasing: Bool
    let brushSize: CGFloat            // in screen points
    @Binding var strokes: [Stroke]
    var onStrokeEnded: () -> Void = {}

    @State private var live: Stroke?

    var body: some View {
        GeometryReader { geo in
            let fit = Self.fitRect(image: image.size, in: geo.size)
            let scale = image.size.width / max(1, fit.width)    // image pixels per screen point
            ZStack(alignment: .topLeading) {
                Image(uiImage: image)
                    .resizable()
                    .frame(width: fit.width, height: fit.height)
                    .offset(x: fit.minX, y: fit.minY)
                if let maskOverlay {
                    Image(uiImage: maskOverlay)
                        .resizable()
                        .frame(width: fit.width, height: fit.height)
                        .offset(x: fit.minX, y: fit.minY)
                        .allowsHitTesting(false)
                }
                if let live {
                    Path { p in
                        let pts = live.points.map { CGPoint(x: $0.x / scale + fit.minX, y: $0.y / scale + fit.minY) }
                        p.addLines(pts)
                    }
                    .stroke(live.erase ? Color.white.opacity(0.6) : Color.red.opacity(0.55),
                            style: StrokeStyle(lineWidth: live.width / scale, lineCap: .round, lineJoin: .round))
                    .allowsHitTesting(false)
                }
            }
            .frame(width: geo.size.width, height: geo.size.height)
            .contentShape(Rectangle())
            .gesture(drag(fit: fit, scale: scale), including: painting ? .all : .none)
        }
    }

    private func drag(fit: CGRect, scale: CGFloat) -> some Gesture {
        DragGesture(minimumDistance: 0)
            .onChanged { v in
                let p = CGPoint(x: (v.location.x - fit.minX) * scale, y: (v.location.y - fit.minY) * scale)
                if live == nil { live = Stroke(points: [], width: brushSize * scale, erase: erasing) }
                live?.points.append(p)
            }
            .onEnded { _ in
                if let s = live { strokes.append(s) }
                live = nil
                onStrokeEnded()
            }
    }

    static func fitRect(image: CGSize, in box: CGSize) -> CGRect {
        guard image.width > 0, image.height > 0 else { return .zero }
        let s = min(box.width / image.width, box.height / image.height)
        let size = CGSize(width: image.width * s, height: image.height * s)
        return CGRect(x: (box.width - size.width) / 2, y: (box.height - size.height) / 2, width: size.width, height: size.height)
    }
}
