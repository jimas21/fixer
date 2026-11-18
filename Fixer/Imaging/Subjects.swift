import CoreGraphics
import CoreImage
import Vision

/// Apple's built-in on-device segmentation (no download): people and general foreground subjects.
enum Subjects {
    enum Failure: LocalizedError {
        case nothingFound
        var errorDescription: String? { "I couldn't find a person or subject there." }
    }

    /// Vision runs on a copy this size (long side) and masks are scaled back up to the photo. Full-size Vision on a
    /// 12-48 MP photo took seconds per request.
    static let workSide: CGFloat = 1024

    static func workCopy(_ image: CGImage) -> CGImage {
        let s = min(1, workSide / CGFloat(max(image.width, image.height)))
        guard s < 1 else { return image }
        return ImageKit.resized(image, to: CGSize(width: (CGFloat(image.width) * s).rounded(),
                                                  height: (CGFloat(image.height) * s).rounded())) ?? image
    }

    /// Mask of every foreground subject (people, pets, objects) — the "lift subject" mask.
    static func allSubjects(in image: CGImage) throws -> CGImage {
        let request = VNGenerateForegroundInstanceMaskRequest()
        let handler = VNImageRequestHandler(cgImage: workCopy(image), options: [:])
        try handler.perform([request])
        guard let obs = request.results?.first, !obs.allInstances.isEmpty else { throw Failure.nothingFound }
        let buffer = try obs.generateScaledMaskForImage(forInstances: obs.allInstances, from: handler)
        guard let mask = ImageKit.cgImage(buffer) else { throw Failure.nothingFound }
        return toGray(mask, width: image.width, height: image.height)
    }

    /// Masks for each separate thing Vision finds: people first (up to 4, split per person), then other subjects.
    /// Masks are at the small working size (see `workSide`); `piece` scales the chosen one up to the photo.
    static func pieces(in image: CGImage) -> [CGImage] {
        var out: [CGImage] = []
        let small = workCopy(image)
        let handler = VNImageRequestHandler(cgImage: small, options: [:])
        let people = VNGeneratePersonInstanceMaskRequest()
        let subjects = VNGenerateForegroundInstanceMaskRequest()
        try? handler.perform([people, subjects])
        for obs in [people.results?.first, subjects.results?.first].compactMap({ $0 }) {
            for index in obs.allInstances {
                if let buffer = try? obs.generateScaledMaskForImage(forInstances: IndexSet(integer: index), from: handler),
                   let mask = ImageKit.cgImage(buffer) {
                    out.append(toGray(mask, width: small.width, height: small.height))
                }
            }
        }
        return out
    }

    /// The piece that best fills `box` (pixels, top-left origin). Falls back to nil when nothing overlaps well,
    /// so the caller can use the box itself.
    static func piece(in image: CGImage, matching box: CGRect) -> CGImage? {
        piece(from: pieces(in: image), photo: image, matching: box)
    }

    /// Same, choosing among `found` (from `pieces(in:)`, computed once per photo). Returns a full-size mask.
    static func piece(from found: [CGImage], photo image: CGImage, matching box: CGRect) -> CGImage? {
        let w = image.width, h = image.height
        // Score on a small grid: fast and plenty precise for picking.
        let sw = 160, sh = max(1, Int(Double(160) * Double(h) / Double(w)))
        let sx = Double(sw) / Double(w), sy = Double(sh) / Double(h)
        let small = CGRect(x: box.minX * sx, y: box.minY * sy, width: box.width * sx, height: box.height * sy)
        var best: (score: Double, mask: CGImage)?
        for mask in found {
            let px = ImageKit.grayBytes(mask, width: sw, height: sh)
            var inside = 0, total = 0
            for y in 0..<sh {
                for x in 0..<sw where px[y * sw + x] > 127 {
                    total += 1
                    if small.contains(CGPoint(x: Double(x) + 0.5, y: Double(y) + 0.5)) { inside += 1 }
                }
            }
            guard total > 0 else { continue }
            let boxArea = max(1, Double(small.width * small.height))
            let precision = Double(inside) / Double(total)      // how much of the piece is in the box
            let recall = min(1, Double(inside) / boxArea)        // how much of the box the piece fills
            let score = 2 * precision * recall / max(0.0001, precision + recall)
            if score > (best?.score ?? 0.25) { best = (score, mask) }
        }
        guard let chosen = best?.mask else { return nil }
        return toGray(chosen, width: w, height: h)
    }

    /// Cut-out: the photo with everything except `mask` made transparent.
    static func cutOut(_ image: CGImage, mask: CGImage) -> CGImage? {
        let img = CIImage(cgImage: image)
        let m = CIImage(cgImage: mask)
        let f = CIFilter(name: "CIBlendWithMask", parameters: [
            kCIInputImageKey: img,
            kCIInputBackgroundImageKey: CIImage(color: .clear).cropped(to: img.extent),
            kCIInputMaskImageKey: m,
        ])
        guard let out = f?.outputImage else { return nil }
        return ImageKit.context.createCGImage(out, from: img.extent, format: .RGBA8,
                                              colorSpace: CGColorSpaceCreateDeviceRGB())
    }

    /// Vision masks come back as float or 8-bit buffers at image size; normalise to 8-bit gray.
    private static func toGray(_ mask: CGImage, width: Int, height: Int) -> CGImage {
        let bytes = ImageKit.grayBytes(mask, width: width, height: height)
        return ImageKit.grayImage(bytes: bytes, width: width, height: height) ?? mask
    }
}
