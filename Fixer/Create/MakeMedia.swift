import CryptoKit
import Foundation
import UIKit

/// 11-22 (memory): the pixel store behind every `CreateResult` in a Make session.
///
/// The problem it solves: making pictures 4 at a time used to keep a full-resolution *decoded* bitmap for every result
/// in RAM. At ~4 MB each that piled up fast — the phone bogged down and iOS eventually jettisoned the app. Keeping the
/// bytes compressed instead only slowed the growth; it was still one copy per result resident, so at a few hundred
/// pictures it hit the same wall.
///
/// The fix: resident memory here is **bounded by two caches** — a decoded full-res cache and a decoded thumbnail cache
/// — so it is flat no matter how many pictures a session holds (70, 700, 7000). The durable copy of each result's
/// pixels lives **off the heap**, in an on-disk scratch that is faulted back into those caches on demand.
///
/// End-to-end encryption is preserved — in fact strengthened:
///   * The scratch files are AES-256-GCM sealed with a key generated at launch that lives **only in this process's
///     memory** and is never written to disk or the keychain. When the app exits the key is gone, so the files are
///     unreadable by anyone — not recoverable at rest. The directory is also wiped on launch and by "Clear everything",
///     and each file carries iOS file protection as a second layer.
///   * Nothing plaintext is written to disk for pictures (a made video waits in tmp so it can play; it is deleted by
///     "Clear everything" and swept at the next launch). The authoritative, restart-surviving copy of a result is still the
///     encrypted Library (per-item key); this scratch is a fast, session-only cache that works even while the Library
///     is locked (Make saves while locked), so you never have to unlock just to scroll back through this session.
///   * Fewer plaintext bitmaps are resident than before, which only reduces exposure in memory.
///
/// Access is synchronous: a miss decrypts and decodes on the calling thread. A full-res decode is a few milliseconds
/// and happens only when an *evicted* older picture is opened; the picture being made is always warm in the cache, so
/// active making never pays it. Thumbnails are tiny and cheap to fault for the roll.
final class MakeMedia: @unchecked Sendable {       // internally synchronized: NSCache (thread-safe), immutable key, serial IO
    static let shared = MakeMedia()

    private let key = SymmetricKey(size: .bits256)        // ephemeral: RAM only, dies with the process
    private let dir: URL
    private let io = DispatchQueue(label: "fixer.makemedia", qos: .utility)

    /// Decoded full-resolution bitmaps. ~96 MB — a couple dozen 1024² pictures — before the oldest are evicted. iOS also
    /// drops the whole cache under memory pressure. Lower the limit for tighter-RAM devices.
    private let full: NSCache<NSString, UIImage> = {
        let c = NSCache<NSString, UIImage>(); c.totalCostLimit = 96 * 1024 * 1024; return c
    }()
    /// Decoded thumbnails for the strips and pickers. ~24 MB (≈ 90 tiles) before eviction; re-faulted from scratch.
    private let thumbs: NSCache<NSString, UIImage> = {
        let c = NSCache<NSString, UIImage>(); c.totalCostLimit = 24 * 1024 * 1024; return c
    }()

    private init() {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("make-media", isDirectory: true)
        try? FileManager.default.removeItem(at: dir)      // nothing from a previous run is readable anyway; start clean
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    // MARK: ingest

    /// Takes a freshly made full-resolution picture: warms the caches (it is about to be shown), and seals its compressed
    /// bytes + thumbnail into the scratch so RAM can drop them later and fault them back. Returns the thumbnail so the
    /// caller can hold nothing itself.
    func ingest(_ id: UUID, _ image: UIImage) {
        let key = id.uuidString as NSString
        let thumb = Self.makeThumb(image)
        full.setObject(image, forKey: key, cost: Self.cost(image))
        thumbs.setObject(thumb, forKey: key, cost: Self.cost(thumb))
        // Compress + seal + write off the main thread; a fault before it lands simply falls back to the warm cache.
        io.async { [weak self] in
            guard let self else { return }
            let hasAlpha = image.cgImage.map { ![.none, .noneSkipLast, .noneSkipFirst].contains($0.alphaInfo) } ?? false
            let data = (hasAlpha ? image.pngData() : (image.jpegData(compressionQuality: 0.95) ?? image.pngData())) ?? Data()
            self.writeSealed(id, "f", data)
            if let t = thumb.jpegData(compressionQuality: 0.8) { self.writeSealed(id, "t", t) }
        }
    }

    // MARK: read (synchronous: cache → sealed scratch → fallback)

    /// The full-resolution picture. Warm from the cache, else decrypt+decode from the scratch (a few ms, only for an
    /// evicted older picture), else the thumbnail as a last resort so something always shows.
    func image(_ id: UUID) -> UIImage {
        fullImage(id) ?? thumb(id)
    }

    /// The real full-resolution picture, or nil when neither the cache nor the scratch still has it. Callers that send
    /// or keep the picture (change, animate) use this so they never pass a thumbnail off as the original.
    func fullImage(_ id: UUID) -> UIImage? {
        let key = id.uuidString as NSString
        if let hit = full.object(forKey: key) { return hit }
        if let d = readSealed(id, "f"), let ui = UIImage(data: d) {
            full.setObject(ui, forKey: key, cost: Self.cost(ui))
            return ui
        }
        return nil
    }

    /// A small thumbnail for the roll and still-picker. Warm from the cache, else decrypt the tiny sealed thumb, else a
    /// blank placeholder (never nil, so the UI has something to draw).
    func thumb(_ id: UUID) -> UIImage {
        let key = id.uuidString as NSString
        if let hit = thumbs.object(forKey: key) { return hit }
        if let d = readSealed(id, "t"), let ui = UIImage(data: d) {
            thumbs.setObject(ui, forKey: key, cost: Self.cost(ui))
            return ui
        }
        return Self.placeholder
    }

    // MARK: lifecycle

    /// Drop one result's pixels (RAM + scratch). Safe to call for an id that was never ingested.
    func drop(_ id: UUID) {
        let key = id.uuidString as NSString
        full.removeObject(forKey: key); thumbs.removeObject(forKey: key)
        io.async { [weak self] in
            guard let self else { return }
            for ext in ["f", "t"] { try? FileManager.default.removeItem(at: self.file(id, ext)) }
        }
    }

    /// "Clear everything": forget every cached bitmap and wipe the whole scratch directory.
    func clearAll() {
        full.removeAllObjects(); thumbs.removeAllObjects()
        io.async { [weak self] in
            guard let self else { return }
            try? FileManager.default.removeItem(at: self.dir)
            try? FileManager.default.createDirectory(at: self.dir, withIntermediateDirectories: true)
        }
    }

    // MARK: sealed scratch

    private func file(_ id: UUID, _ ext: String) -> URL { dir.appendingPathComponent(id.uuidString + "." + ext) }

    private func writeSealed(_ id: UUID, _ ext: String, _ data: Data) {
        guard !data.isEmpty, let sealed = try? AES.GCM.seal(data, using: key).combined else { return }
        // "Clear everything" (and the system's tmp cleanup) can remove the folder; make sure it is there before writing.
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try? sealed.write(to: file(id, ext), options: [.atomic, .completeFileProtectionUnlessOpen])
    }

    private func readSealed(_ id: UUID, _ ext: String) -> Data? {
        guard let blob = try? Data(contentsOf: file(id, ext)),
              let box = try? AES.GCM.SealedBox(combined: blob),
              let plain = try? AES.GCM.open(box, using: key) else { return nil }
        return plain
    }

    // MARK: helpers

    private static func cost(_ img: UIImage) -> Int {
        Int(img.size.width * img.scale * img.size.height * img.scale) * 4
    }

    private static func makeThumb(_ img: UIImage, maxSide: CGFloat = 256) -> UIImage {
        let w = img.size.width, h = img.size.height
        let longest = Swift.max(w, h)
        guard longest > maxSide, longest > 0 else { return img }
        let scale = maxSide / longest
        let size = CGSize(width: w * scale, height: h * scale)
        let fmt = UIGraphicsImageRendererFormat.default()
        fmt.scale = 1                                       // 1 px per point: the tiles are tiny
        let hasAlpha = img.cgImage.map { ![.none, .noneSkipLast, .noneSkipFirst].contains($0.alphaInfo) } ?? false
        fmt.opaque = !hasAlpha
        return UIGraphicsImageRenderer(size: size, format: fmt).image { _ in
            img.draw(in: CGRect(origin: .zero, size: size))
        }
    }

    private static let placeholder: UIImage = {
        let fmt = UIGraphicsImageRendererFormat.default(); fmt.opaque = true
        return UIGraphicsImageRenderer(size: CGSize(width: 2, height: 2), format: fmt).image { ctx in
            UIColor.secondarySystemBackground.setFill(); ctx.fill(CGRect(x: 0, y: 0, width: 2, height: 2))
        }
    }()
}
