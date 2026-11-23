import CoreML
import CoreGraphics
import StableDiffusion

enum ToolError: LocalizedError {
    case noMask, model(String), blocked
    var errorDescription: String? {
        switch self {
        case .noMask: "Paint over the area to change, or describe it."
        case .model(let s): s
        case .blocked: "This result was blocked by your content filter. You can adjust it in Settings."
        }
    }
}

private func compiledModel(_ m: CoreModel) throws -> MLModel {
    let config = MLModelConfiguration()
    config.computeUnits = .all
    do { return try MLModel(contentsOf: ModelStore.compiled(m), configuration: config) }
    catch { throw ToolError.model("Couldn't load the \(m.title.lowercased()). Try deleting and downloading it again in Settings › Storage.") }
}

/// A square-ish window around the mask, padded for context, clamped to the image.
private func workWindow(around box: CGRect, in size: CGSize, minSide: CGFloat) -> CGRect {
    let side = min(max(max(box.width, box.height) * 2.2, minSide), max(size.width, size.height))
    var r = CGRect(x: box.midX - side / 2, y: box.midY - side / 2, width: side, height: side)
    r.size.width = min(r.width, size.width)
    r.size.height = min(r.height, size.height)
    r.origin.x = min(max(0, r.minX), size.width - r.width)
    r.origin.y = min(max(0, r.minY), size.height - r.height)
    return r.integral
}

/// Pastes `patch` into `rect` of `base`, through `mask` (full-size, soft).
private func paste(_ patch: CGImage, into base: CGImage, at rect: CGRect, mask: CGImage) -> CGImage? {
    let w = base.width, h = base.height
    guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                              space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)
    else { return nil }
    ctx.draw(base, in: CGRect(x: 0, y: 0, width: w, height: h))
    // CGContext origin is bottom-left; rect is top-left based.
    ctx.draw(patch, in: CGRect(x: rect.minX, y: CGFloat(h) - rect.maxY, width: rect.width, height: rect.height))
    guard let full = ctx.makeImage() else { return nil }
    return ImageKit.blend(base: base, patch: full, mask: mask)
}

// MARK: - Remove (LaMa)

enum Remover {
    static func remove(_ image: CGImage, mask rawMask: CGImage) async throws -> CGImage {
        try await ModelStore.shared.ensure(.remover)
        // A detached task does not inherit Stop, so cancel it by hand.
        let work = Task.detached(priority: .userInitiated) { () throws -> CGImage in
            let size = CGSize(width: image.width, height: image.height)
            let hole = ImageKit.softened(rawMask, grow: max(6, CGFloat(image.width) / 200), feather: 0)
            guard let box = ImageKit.maskBounds(hole) else { throw ToolError.noMask }
            let window = workWindow(around: box, in: size, minSide: 512)
            let side = CGSize(width: 800, height: 800)
            guard let crop = ImageKit.cropped(image, to: window), let cropMask = ImageKit.cropped(hole, to: window),
                  let inPB = ImageKit.pixelBuffer(crop, size: side), let maskPB = ImageKit.grayPixelBuffer(cropMask, size: side)
            else { throw ToolError.model("Couldn't prepare the photo.") }
            let model = try compiledModel(.remover)
            try Task.checkCancellation()
            let input = try MLDictionaryFeatureProvider(dictionary: [
                "image": MLFeatureValue(pixelBuffer: inPB),
                "mask": MLFeatureValue(pixelBuffer: maskPB),
            ])
            let out = try model.prediction(from: input)
            guard let pb = out.featureValue(for: "output")?.imageBufferValue, let filled = ImageKit.cgImage(pb),
                  let back = ImageKit.resized(filled, to: window.size) else { throw ToolError.model("Couldn't remove that. Try again.") }
            let blendMask = ImageKit.softened(hole, grow: 2, feather: 3)
            guard let result = paste(back, into: image, at: window, mask: blendMask) else { throw ToolError.model("Couldn't finish the edit. Try again.") }
            return result
        }
        return try await withTaskCancellationHandler { try await work.value } onCancel: { work.cancel() }
    }
}

// MARK: - Upscale (Real-ESRGAN x4, 512 → 2048 tiles)

enum Upscaler {
    static let tile = 512, scale = 4, overlap = 32

    static func upscale(_ source: CGImage, progress: @escaping @Sendable (Double) -> Void) async throws -> CGImage {
        try await ModelStore.shared.ensure(.upscaler)
        // A detached task does not inherit Stop, so cancel it by hand (the tile loop checks).
        let work = Task.detached(priority: .userInitiated) { () throws -> CGImage in
            // Keep the result phone-sized: at most 4096 px on the long side.
            var image = source
            let longSide = max(image.width, image.height)
            if longSide > 1024 {
                let s = 1024.0 / Double(longSide)
                image = ImageKit.resized(image, to: CGSize(width: (Double(image.width) * s).rounded(),
                                                           height: (Double(image.height) * s).rounded())) ?? image
            }
            let model = try compiledModel(.upscaler)
            let w = image.width, h = image.height
            let ow = w * scale, oh = h * scale
            guard let out = CGContext(data: nil, width: ow, height: oh, bitsPerComponent: 8, bytesPerRow: 0,
                                      space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)
            else { throw ToolError.model("Not enough memory to upscale.") }
            let step = tile - overlap * 2
            let xs = stride(from: 0, to: max(1, w - overlap * 2), by: step).map { min($0, max(0, w - tile)) }
            let ys = stride(from: 0, to: max(1, h - overlap * 2), by: step).map { min($0, max(0, h - tile)) }
            let total = Double(xs.count * ys.count)
            var done = 0.0
            for ty in Array(Set(ys)).sorted() {
                for tx in Array(Set(xs)).sorted() {
                    try Task.checkCancellation()
                    let tw = min(tile, w - tx), th = min(tile, h - ty)
                    // Pad small/edge tiles onto a 512 canvas (top-left aligned).
                    guard let crop = image.cropping(to: CGRect(x: tx, y: ty, width: tw, height: th)),
                          let canvas = CGContext(data: nil, width: tile, height: tile, bitsPerComponent: 8, bytesPerRow: 0,
                                                 space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)
                    else { continue }
                    canvas.draw(crop, in: CGRect(x: 0, y: tile - th, width: tw, height: th))
                    guard let padded = canvas.makeImage(), let pb = ImageKit.pixelBuffer(padded) else { continue }
                    let input = try MLDictionaryFeatureProvider(dictionary: ["input": MLFeatureValue(pixelBuffer: pb)])
                    let result = try model.prediction(from: input)
                    guard let opb = result.featureValue(for: "activation_out")?.imageBufferValue,
                          let big = ImageKit.cgImage(opb) else { continue }
                    // Keep only the tile's own centre (drop the overlap margin, except at image edges).
                    let left = tx == 0 ? 0 : overlap, top = ty == 0 ? 0 : overlap
                    let right = tx + tw >= w ? tw : tw - overlap, bottom = ty + th >= h ? th : th - overlap
                    let keep = CGRect(x: left * scale, y: top * scale, width: (right - left) * scale, height: (bottom - top) * scale)
                    guard keep.width > 0, keep.height > 0, let piece = big.cropping(to: keep) else { continue }
                    let dx = (tx + left) * scale, dy = (ty + top) * scale
                    out.draw(piece, in: CGRect(x: dx, y: oh - dy - Int(keep.height), width: Int(keep.width), height: Int(keep.height)))
                    done += 1
                    progress(done / total)
                }
            }
            guard let final = out.makeImage() else { throw ToolError.model("Couldn't finish upscaling. Try again.") }
            return final
        }
        return try await withTaskCancellationHandler { try await work.value } onCancel: { work.cancel() }
    }
}

// MARK: - Generate / redraw (Stable Diffusion)

actor Generator {
    static let shared = Generator()
    private var pipeline: StableDiffusionPipeline?
    private var loadedFor: String?

    private func ready() throws -> StableDiffusionPipeline {
        let id = ModelStore.folder(.generator).path
        if let pipeline, loadedFor == id { return pipeline }
        let config = MLModelConfiguration()
        config.computeUnits = .cpuAndNeuralEngine
        let p = try StableDiffusionPipeline(resourcesAt: ModelStore.folder(.generator), controlNet: [],
                                            configuration: config, disableSafety: true, reduceMemory: true)
        pipeline = p
        loadedFor = id
        return p
    }

    func unload() { pipeline = nil; loadedFor = nil }

    struct Settings: Sendable {
        var prompt: String
        var negative: String = "blurry, deformed, disfigured, bad anatomy, extra fingers, watermark, text"
        var steps: Int
        var guidance: Double
        var strength: Double
        var remote = false                                   // run on the owner's PC (Fixer Desktop)
        var status: @Sendable (String) -> Void = { _ in }
        var slot: String? = nil                              // 11-17: the place this comes from (a new one replaces it)
        var queued: @Sendable (String) -> Void = { _ in }    // 11-17: the computer's job id once it is in line
        var explicit: Bool? = nil                            // 11-17: the chat model's call: nude or sexual (nil = not said)
        var enhance = false                                  // 11-17: ask the computer to enrich the prompt first
        var preview: @Sendable (Data) -> Void = { _ in }     // a half-drawn picture from the computer (JPEG), if it sends any
        var produced: @Sendable (PCImageInfo) -> Void = { _ in }   // called only when the computer itself drew the picture
    }

    /// One 512×512 image. `start` makes it an edit of that picture.
    private func render(_ s: Settings, start: CGImage?, progress: @escaping @Sendable (Double) -> Void) async throws -> CGImage {
        if s.remote {
            return try await Desktop.shared.image(prompt: s.prompt, start: start, slot: s.slot, explicit: s.explicit,
                                                  enhance: s.enhance, queued: s.queued, status: s.status, progress: progress,
                                                  preview: s.preview, produced: s.produced)
        }
        if pipeline == nil {
            s.status("Preparing image model (first use can take a minute)…")
        }
        let p = try ready()
        s.status(start == nil ? "Creating image…" : "Editing…")
        var c = StableDiffusionPipeline.Configuration(prompt: s.prompt)
        c.negativePrompt = s.negative
        c.stepCount = s.steps
        c.guidanceScale = Float(s.guidance)
        c.seed = UInt32.random(in: 0...UInt32.max)
        c.disableSafety = true
        c.schedulerType = .dpmSolverMultistepScheduler
        if let start {
            c.startingImage = ImageKit.resized(start, to: CGSize(width: 512, height: 512))
            c.strength = Float(s.strength)
        }
        let steps = Double(max(1, s.steps))
        let images = try p.generateImages(configuration: c) { prog in
            progress(Double(prog.step) / steps)
            return !Task.isCancelled
        }
        guard let first = images.first, let img = first else { throw ToolError.model("No image was produced. Try again.") }
        return img
    }

    /// A new image from a description.
    func make(_ s: Settings, filter: FilterSetting, progress: @escaping @Sendable (Double) -> Void) async throws -> CGImage {
        let img = try await render(s, start: nil, progress: progress)
        if Detector.blocks(img, filter) { throw ToolError.blocked }
        return img
    }

    /// Redraws the masked part of a photo (or the whole photo if `mask` is nil) following the prompt.
    func redraw(_ photo: CGImage, mask: CGImage?, _ s: Settings, filter: FilterSetting,
                progress: @escaping @Sendable (Double) -> Void) async throws -> CGImage {
        let size = CGSize(width: photo.width, height: photo.height)
        if let mask {
            guard let box = ImageKit.maskBounds(mask) else { throw ToolError.noMask }
            let window = workWindow(around: box, in: size, minSide: s.remote ? 768 : 384)
            guard let crop = ImageKit.cropped(photo, to: window) else { throw ToolError.noMask }
            let out = try await render(s, start: crop, progress: progress)
            guard let back = ImageKit.resized(out, to: window.size) else { throw ToolError.model("Couldn't finish the edit. Try again.") }
            let soft = ImageKit.softened(mask, grow: 4, feather: max(4, CGFloat(photo.width) / 250))
            guard let result = paste(back, into: photo, at: window, mask: soft) else { throw ToolError.model("Couldn't finish the edit. Try again.") }
            if Detector.blocks(back, filter) { throw ToolError.blocked }
            return result
        }
        let out = try await render(s, start: photo, progress: progress)
        guard let result = ImageKit.resized(out, to: size) else { throw ToolError.model("Couldn't finish the edit. Try again.") }
        if Detector.blocks(out, filter) { throw ToolError.blocked }
        return result
    }
}
