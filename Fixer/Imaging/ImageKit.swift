import CoreGraphics
import CoreImage
import CoreImage.CIFilterBuiltins
import CoreVideo
import UIKit

/// Small, dependency-free image helpers shared by every tool.
/// Convention: a "mask" is a grayscale CGImage the same size as the photo; white = the area to change.
enum ImageKit {
    static let context = CIContext(options: [.cacheIntermediates: false])

    /// Bakes the EXIF orientation into the pixels so every tool sees an upright `.up` image.
    static func upright(_ image: UIImage, maxSide: CGFloat = 4096) -> UIImage {
        let size = image.size
        let scale = min(1, maxSide / max(size.width, size.height))
        let target = CGSize(width: (size.width * scale).rounded(), height: (size.height * scale).rounded())
        if image.imageOrientation == .up, scale == 1, image.cgImage != nil { return image }
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        format.opaque = true
        return UIGraphicsImageRenderer(size: target, format: format).image { _ in
            image.draw(in: CGRect(origin: .zero, size: target))
        }
    }

    static func cgImage(_ image: UIImage) -> CGImage? {
        if let cg = image.cgImage { return cg }
        if let ci = image.ciImage { return context.createCGImage(ci, from: ci.extent) }
        return nil
    }

    /// Draws `image` into a new RGB bitmap of `size` (aspect is not preserved).
    static func resized(_ image: CGImage, to size: CGSize) -> CGImage? {
        let w = Int(size.width), h = Int(size.height)
        guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                                  space: CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { return nil }
        ctx.interpolationQuality = .high
        ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
        return ctx.makeImage()
    }

    static func cropped(_ image: CGImage, to rect: CGRect) -> CGImage? {
        image.cropping(to: rect.integral)
    }

    /// 32BGRA pixel buffer for Core ML image inputs.
    static func pixelBuffer(_ image: CGImage, size: CGSize? = nil) -> CVPixelBuffer? {
        let w = Int(size?.width ?? CGFloat(image.width)), h = Int(size?.height ?? CGFloat(image.height))
        var pb: CVPixelBuffer?
        let attrs = [kCVPixelBufferCGImageCompatibilityKey: true,
                     kCVPixelBufferCGBitmapContextCompatibilityKey: true] as CFDictionary
        guard CVPixelBufferCreate(kCFAllocatorDefault, w, h, kCVPixelFormatType_32BGRA, attrs, &pb) == kCVReturnSuccess,
              let buffer = pb else { return nil }
        CVPixelBufferLockBaseAddress(buffer, [])
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
        guard let ctx = CGContext(data: CVPixelBufferGetBaseAddress(buffer), width: w, height: h, bitsPerComponent: 8,
                                  bytesPerRow: CVPixelBufferGetBytesPerRow(buffer), space: CGColorSpaceCreateDeviceRGB(),
                                  bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue)
        else { return nil }
        ctx.interpolationQuality = .high
        ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
        return buffer
    }

    /// One-channel 8-bit pixel buffer (for mask inputs).
    static func grayPixelBuffer(_ mask: CGImage, size: CGSize) -> CVPixelBuffer? {
        let w = Int(size.width), h = Int(size.height)
        var pb: CVPixelBuffer?
        guard CVPixelBufferCreate(kCFAllocatorDefault, w, h, kCVPixelFormatType_OneComponent8, nil, &pb) == kCVReturnSuccess,
              let buffer = pb else { return nil }
        CVPixelBufferLockBaseAddress(buffer, [])
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
        guard let ctx = CGContext(data: CVPixelBufferGetBaseAddress(buffer), width: w, height: h, bitsPerComponent: 8,
                                  bytesPerRow: CVPixelBufferGetBytesPerRow(buffer), space: CGColorSpaceCreateDeviceGray(),
                                  bitmapInfo: CGImageAlphaInfo.none.rawValue) else { return nil }
        ctx.interpolationQuality = .high
        ctx.draw(mask, in: CGRect(x: 0, y: 0, width: w, height: h))
        return buffer
    }

    static func cgImage(_ buffer: CVPixelBuffer) -> CGImage? {
        let ci = CIImage(cvPixelBuffer: buffer)
        return context.createCGImage(ci, from: ci.extent)
    }

    /// Grayscale bytes of a mask at a given size (row-major, top row first).
    static func grayBytes(_ mask: CGImage, width: Int, height: Int) -> [UInt8] {
        var bytes = [UInt8](repeating: 0, count: width * height)
        bytes.withUnsafeMutableBytes { raw in
            guard let ctx = CGContext(data: raw.baseAddress, width: width, height: height, bitsPerComponent: 8,
                                      bytesPerRow: width, space: CGColorSpaceCreateDeviceGray(),
                                      bitmapInfo: CGImageAlphaInfo.none.rawValue) else { return }
            ctx.interpolationQuality = .none
            ctx.draw(mask, in: CGRect(x: 0, y: 0, width: width, height: height))
        }
        return bytes
    }

    static func grayImage(bytes: [UInt8], width: Int, height: Int) -> CGImage? {
        let data = Data(bytes) as CFData
        guard let provider = CGDataProvider(data: data) else { return nil }
        return CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 8, bytesPerRow: width,
                       space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGBitmapInfo(rawValue: 0),
                       provider: provider, decode: nil, shouldInterpolate: true, intent: .defaultIntent)
    }

    /// Bounding box (pixels, top-left origin) of the white area of a mask, or nil if the mask is empty.
    static func maskBounds(_ mask: CGImage) -> CGRect? {
        let w = mask.width, h = mask.height
        let bytes = grayBytes(mask, width: w, height: h)
        var minX = w, minY = h, maxX = -1, maxY = -1
        for y in 0..<h {
            let row = y * w
            for x in 0..<w where bytes[row + x] > 127 {
                if x < minX { minX = x }
                if x > maxX { maxX = x }
                if y < minY { minY = y }
                if y > maxY { maxY = y }
            }
        }
        guard maxX >= 0 else { return nil }
        return CGRect(x: minX, y: minY, width: maxX - minX + 1, height: maxY - minY + 1)
    }

    /// Grows (dilates) and softens a mask so edits blend instead of leaving a hard seam.
    static func softened(_ mask: CGImage, grow: CGFloat, feather: CGFloat) -> CGImage {
        var ci = CIImage(cgImage: mask)
        let extent = ci.extent
        if grow > 0 {
            let f = CIFilter.morphologyMaximum()
            f.inputImage = ci
            f.radius = Float(grow)
            ci = f.outputImage ?? ci
        }
        if feather > 0 {
            ci = ci.clampedToExtent().applyingGaussianBlur(sigma: Double(feather)).cropped(to: extent)
        }
        return context.createCGImage(ci, from: extent, format: .L8, colorSpace: CGColorSpaceCreateDeviceGray()) ?? mask
    }

    /// Puts `patch` over `base` only where `mask` is white (soft edges respected). All three share base's size.
    static func blend(base: CGImage, patch: CGImage, mask: CGImage) -> CGImage? {
        let b = CIImage(cgImage: base)
        var p = CIImage(cgImage: patch)
        var m = CIImage(cgImage: mask)
        let extent = b.extent
        if p.extent.size != extent.size {
            p = p.transformed(by: CGAffineTransform(scaleX: extent.width / p.extent.width, y: extent.height / p.extent.height))
        }
        if m.extent.size != extent.size {
            m = m.transformed(by: CGAffineTransform(scaleX: extent.width / m.extent.width, y: extent.height / m.extent.height))
        }
        let f = CIFilter.blendWithMask()
        f.inputImage = p
        f.backgroundImage = b
        f.maskImage = m
        guard let out = f.outputImage else { return nil }
        return context.createCGImage(out, from: extent)
    }

    /// Mask covering a rectangle (pixels, top-left origin) of an image of `size`.
    static func rectMask(_ rect: CGRect, size: CGSize) -> CGImage? {
        let w = Int(size.width), h = Int(size.height)
        var bytes = [UInt8](repeating: 0, count: w * h)
        let r = rect.integral.intersection(CGRect(x: 0, y: 0, width: w, height: h))
        guard !r.isNull, r.width > 0, r.height > 0 else { return nil }
        for y in Int(r.minY)..<Int(r.maxY) {
            for x in Int(r.minX)..<Int(r.maxX) { bytes[y * w + x] = 255 }
        }
        return grayImage(bytes: bytes, width: w, height: h)
    }

    /// Union of two masks of the same size.
    static func union(_ a: CGImage?, _ b: CGImage?) -> CGImage? {
        guard let a else { return b }
        guard let b else { return a }
        let w = a.width, h = a.height
        let x = grayBytes(a, width: w, height: h), y = grayBytes(b, width: w, height: h)
        return grayImage(bytes: zip(x, y).map { max($0, $1) }, width: w, height: h)
    }

    /// Red see-through overlay of a mask, for showing the user what will change.
    static func overlay(_ mask: CGImage) -> UIImage? {
        let ci = CIImage(cgImage: mask)
        let red = CIImage(color: CIColor(red: 1, green: 0.15, blue: 0.2, alpha: 0.55)).cropped(to: ci.extent)
        let clear = CIImage(color: .clear).cropped(to: ci.extent)
        let f = CIFilter.blendWithMask()
        f.inputImage = red
        f.backgroundImage = clear
        f.maskImage = ci
        guard let out = f.outputImage, let cg = context.createCGImage(out, from: ci.extent) else { return nil }
        return UIImage(cgImage: cg)
    }
}
