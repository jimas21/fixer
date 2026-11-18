import CryptoKit
import Foundation
import UIKit

/// The Library's files. Nothing here touches screen state, so it can run off the main thread.
///
/// Application Support/Library/ holds, per item <uuid>:
///   .pic or .vid  the picture or video, sealed
///   .meta         the words, model, seed, a small thumbnail (sealed)
///   .key          the item's own key, sealed (written LAST: an item exists only once its key is on disk)
///   .protected    an empty marker; present means Protected
/// Kind, protected and size all come from file names, so counting needs no key. Everything else is sealed.
enum LibraryVault {
    struct Stored {
        var meta: LibraryMeta
        var dek: SymmetricKey
        var inventory: LibraryInventory
    }

    struct Loaded {
        var meta: LibraryMeta
        var dek: SymmetricKey
        var isProtected: Bool
    }

    static var root: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Library", isDirectory: true)
    }

    static func file(_ id: UUID, _ ext: String) -> URL { root.appendingPathComponent(id.uuidString + "." + ext) }

    private static let options: Data.WritingOptions = [.atomic, .completeFileProtectionUnlessOpen]

    // MARK: counting (file names only)

    private struct Listing {
        var keys: Set<String> = []
        var pics: [String: Int64] = [:]
        var vids: [String: Int64] = [:]
        var protected: Set<String> = []
        var others: Set<String> = []          // stems seen at all
    }

    private static func listing() -> Listing {
        var l = Listing()
        let urls = (try? FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: [.fileSizeKey])) ?? []
        for url in urls {
            let stem = url.deletingPathExtension().lastPathComponent
            let size = Int64((try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0)
            l.others.insert(stem)
            switch url.pathExtension {
            case "key": l.keys.insert(stem)
            case "pic": l.pics[stem] = size
            case "vid": l.vids[stem] = size
            case "protected": l.protected.insert(stem)
            default: break
            }
        }
        return l
    }

    static func inventory() -> LibraryInventory {
        let l = listing()
        var inv = LibraryInventory()
        for stem in l.keys {
            if let s = l.pics[stem] { inv.pictures += 1; inv.bytes += s }
            else if let s = l.vids[stem] { inv.videos += 1; inv.bytes += s }
            else { continue }
            if l.protected.contains(stem) { inv.protected += 1 }
        }
        return inv
    }

    /// Item ids that are not protected (the ones Clear everything erases).
    static func unprotectedIDs() -> [UUID] {
        let l = listing()
        return l.keys.filter { !l.protected.contains($0) }.compactMap { UUID(uuidString: $0) }
    }

    /// Data or meta files whose key never got written (a crash mid-save): nobody can read them, so they go.
    /// Only call this before any save has started (at launch).
    static func sweepOrphans() {
        let l = listing()
        for stem in l.others where !l.keys.contains(stem) {
            for ext in ["pic", "vid", "meta", "protected"] {
                try? FileManager.default.removeItem(at: root.appendingPathComponent(stem + "." + ext))
            }
        }
    }

    // MARK: saving

    private static func thumbnail(_ image: UIImage) -> Data? {
        let longest = max(image.size.width, image.size.height)
        guard longest > 0 else { return nil }
        let scale = min(1, 320 / longest)
        let size = CGSize(width: max(1, (image.size.width * scale).rounded()), height: max(1, (image.size.height * scale).rounded()))
        let format = UIGraphicsImageRendererFormat.default()
        format.scale = 1
        let small = UIGraphicsImageRenderer(size: size, format: format).image { _ in
            image.draw(in: CGRect(origin: .zero, size: size))
        }
        return small.jpegData(compressionQuality: 0.7)
    }

    static func storePicture(_ image: UIImage, meta: LibraryMeta, master: SymmetricKey?) -> Stored? {
        let alpha = image.cgImage.map { ![.none, .noneSkipLast, .noneSkipFirst].contains($0.alphaInfo) } ?? false
        guard let data = alpha ? image.pngData() : image.jpegData(compressionQuality: 0.92) else { return nil }
        var m = meta
        m.width = Int(image.size.width * image.scale)
        m.height = Int(image.size.height * image.scale)
        m.thumbnail = thumbnail(image)
        return store(data, ext: "pic", meta: m, master: master)
    }

    static func storeVideo(_ data: Data, meta: LibraryMeta, thumbnail: UIImage?, master: SymmetricKey?) -> Stored? {
        var m = meta
        if let thumbnail {
            m.width = Int(thumbnail.size.width * thumbnail.scale)
            m.height = Int(thumbnail.size.height * thumbnail.scale)
            m.thumbnail = Self.thumbnail(thumbnail)
        }
        return store(data, ext: "vid", meta: m, master: master)
    }

    private static func store(_ data: Data, ext: String, meta: LibraryMeta, master: SymmetricKey?) -> Stored? {
        let id = meta.id
        let dek = SymmetricKey(size: .bits256)
        do {
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
            try LibraryKeys.seal(data, with: dek).write(to: file(id, ext), options: options)
            try LibraryKeys.seal(try JSONEncoder().encode(meta), with: dek).write(to: file(id, "meta"), options: options)
            let wrapped = try LibraryKeys.wrapNew(dek, id: id, master: master)
            try JSONEncoder().encode(wrapped).write(to: file(id, "key"), options: options)
        } catch {
            for e in ["pic", "vid", "meta", "key"] { try? FileManager.default.removeItem(at: file(id, e)) }
            return nil
        }
        return Stored(meta: meta, dek: dek, inventory: inventory())
    }

    // MARK: opening

    /// Opens every item's key (re-sealing the "se" and "s" ones under the master key so later reads are fast) and its
    /// meta. Items that can't be opened are left out.
    static func openAll(master: SymmetricKey, priv: SecureEnclave.P256.KeyAgreement.PrivateKey?) -> [Loaded] {
        let l = listing()
        var out: [Loaded] = []
        for stem in l.keys {
            guard let id = UUID(uuidString: stem),
                  let keyData = try? Data(contentsOf: file(id, "key")),
                  let wrapped = try? JSONDecoder().decode(LibraryWrapped.self, from: keyData),
                  let dek = LibraryKeys.unwrap(wrapped, id: id, master: master, priv: priv) else { continue }
            if wrapped.w != "k", let again = LibraryKeys.rewrap(dek, under: master),
               let data = try? JSONEncoder().encode(again) {
                try? data.write(to: file(id, "key"), options: options)
            }
            guard let sealed = try? Data(contentsOf: file(id, "meta")),
                  let json = try? LibraryKeys.open(sealed, with: dek),
                  let meta = try? JSONDecoder().decode(LibraryMeta.self, from: json) else { continue }
            out.append(Loaded(meta: meta, dek: dek, isProtected: l.protected.contains(stem)))
        }
        return out
    }

    static func read(_ id: UUID, kind: LibraryKind, dek: SymmetricKey) -> Data? {
        let ext = kind == .picture ? "pic" : "vid"
        guard let sealed = try? Data(contentsOf: file(id, ext)) else { return nil }
        return try? LibraryKeys.open(sealed, with: dek)
    }

    // MARK: protect and delete

    static func setProtected(_ id: UUID, _ on: Bool) {
        let url = file(id, "protected")
        if on { try? Data().write(to: url, options: options) } else { try? FileManager.default.removeItem(at: url) }
    }

    /// The key goes first, so whatever is left behind can't be read by anyone.
    static func delete(_ id: UUID) {
        for e in ["key", "pic", "vid", "meta", "protected"] { try? FileManager.default.removeItem(at: file(id, e)) }
    }

    // MARK: decrypted copies

    static func tempVideoURL(_ id: UUID) -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent("lib-\(id.uuidString).mp4")
    }

    static func removeTempVideos() {
        let tmp = FileManager.default.temporaryDirectory
        for url in (try? FileManager.default.contentsOfDirectory(at: tmp, includingPropertiesForKeys: nil)) ?? []
        where url.lastPathComponent.hasPrefix("lib-") {
            try? FileManager.default.removeItem(at: url)
        }
    }
}
