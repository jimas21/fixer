import CryptoKit
import Foundation

/// Sealed files outside the Library: conversations, the pictures and videos inside them, and songs.
///
/// Each file is one blob: a small header holding the file's own random key (sealed the same way a Library item's key
/// is: to the device's Secure Enclave key, or under the Library's master key when it is open), then the content
/// under AES-256-GCM. One file, written atomically, so a crash can never leave content without its key.
///
/// Who can read a file:
///   * this run of the app, for files it wrote itself or was told to keep (the keys stay in memory, never on disk),
///     so the chat on screen keeps working with no prompt, also after switching away and back;
///   * anyone after Face ID or the passcode (the Library's unlock), until the Library locks again.
/// Writing never needs the owner, so saving works while everything is locked.
///
/// A file written by an older build has no header: it is returned as it is, and `seal(inPlace:)` upgrades it.
enum SealedFiles {
    private static let magic = Data("FXS1".utf8)
    private static let options: Data.WritingOptions = [.atomic, .completeFileProtectionUnlessOpen]
    private static let tempPrefix = "seal-"

    private struct Header: Codable {
        var id: UUID                 // the salt the key was sealed with
        var w: LibraryWrapped
    }

    private static let lock = NSLock()
    private static var kept: [String: SymmetricKey] = [:]      // this run's own files: stay until the app ends
    private static var opened: [String: SymmetricKey] = [:]    // opened with the master key: dropped on lock
    private static var master: SymmetricKey?
    private static var priv: SecureEnclave.P256.KeyAgreement.PrivateKey?

    private static func name(_ url: URL) -> String { url.standardizedFileURL.path }

    // MARK: lock and unlock (driven by LibraryStore)

    static var isOpen: Bool { lock.withLock { master != nil } }

    static func unlocked(master: SymmetricKey, priv: SecureEnclave.P256.KeyAgreement.PrivateKey?) {
        lock.withLock { self.master = master; self.priv = priv }
    }

    /// Drops the master key and every key that was opened with it, and deletes the decrypted temporary copies.
    /// Keys of this run's own files stay, so the conversation on screen survives a trip to another app.
    static func lockNow() {
        lock.withLock { master = nil; priv = nil; opened = [:]; generation += 1 }
        removeTempCopies()
    }

    /// Forgets every key held in memory (Clear everything, Remove everywhere).
    static func forgetAll() {
        lock.withLock { kept = [:]; opened = [:]; generation += 1 }
        removeTempCopies()
    }

    private static var generation = 0          // bumped by every lock: a key opened before it is not remembered after

    // MARK: parsing

    private static func split(_ blob: Data) -> (header: Header, body: Data)? {
        let b = Data(blob)                                   // indices from 0
        guard b.count > 8, b.prefix(4) == magic else { return nil }
        let n = Int(b[4]) << 24 | Int(b[5]) << 16 | Int(b[6]) << 8 | Int(b[7])
        guard n > 0, n < 65_536, b.count > 8 + n,
              let header = try? JSONDecoder().decode(Header.self, from: b.subdata(in: 8..<(8 + n))) else { return nil }
        return (header, b.subdata(in: (8 + n)..<b.count))
    }

    private static func join(_ header: Header, _ body: Data) throws -> Data {
        let h = try JSONEncoder().encode(header)
        var out = magic
        out.append(contentsOf: [UInt8(h.count >> 24 & 0xff), UInt8(h.count >> 16 & 0xff), UInt8(h.count >> 8 & 0xff), UInt8(h.count & 0xff)])
        out.append(h)
        out.append(body)
        return out
    }

    /// True when the file on disk is sealed (false for a missing file or one an older build wrote in the clear).
    static func isSealed(_ url: URL) -> Bool {
        guard let h = try? FileHandle(forReadingFrom: url) else { return false }
        defer { try? h.close() }
        return (try? h.read(upToCount: 4)) == magic
    }

    /// The file's key, if this run may have it: kept, already opened, or openable right now.
    private static func key(for url: URL, header: Header) -> SymmetricKey? {
        let n = name(url)
        let (hit, master, priv, gen) = lock.withLock { (kept[n] ?? opened[n], self.master, self.priv, generation) }
        if let hit { return hit }
        // "s" is the plain fallback used only when the phone has no passcode: there is nothing to unlock with.
        let with = master ?? (header.w.w == "s" ? SymmetricKey(size: .bits256) : nil)
        guard let with, let dek = LibraryKeys.unwrap(header.w, id: header.id, master: with, priv: priv) else { return nil }
        lock.withLock { if gen == generation { opened[n] = dek } }     // a lock in between: don't remember it
        return dek
    }

    // MARK: reading

    /// The file's content. nil when the file is missing, damaged, or sealed and not readable right now (`isLocked`).
    static func read(_ url: URL) -> Data? {
        guard let blob = try? Data(contentsOf: url) else { return nil }
        guard let (header, body) = split(blob) else { return blob }        // an older build's plain file
        guard let dek = key(for: url, header: header) else { return nil }
        return try? LibraryKeys.open(body, with: dek)
    }

    /// True when the file is there and sealed but can't be opened until the owner unlocks.
    static func isLocked(_ url: URL) -> Bool {
        guard let header = header(of: url) else { return false }
        return key(for: url, header: header) == nil
    }

    /// Only the header of a sealed file (a few hundred bytes), without reading its content. nil for a missing,
    /// plain or damaged file.
    private static func header(of url: URL) -> Header? {
        guard let h = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? h.close() }
        guard let head = try? h.read(upToCount: 8), head.count == 8, head.prefix(4) == magic else { return nil }
        let b = Data(head)
        let n = Int(b[4]) << 24 | Int(b[5]) << 16 | Int(b[6]) << 8 | Int(b[7])
        guard n > 0, n < 65_536, let raw = try? h.read(upToCount: n), raw.count == n else { return nil }
        return try? JSONDecoder().decode(Header.self, from: raw)
    }

    // MARK: writing

    /// Seals `data` and writes it (atomically). Works while locked. Returns false when nothing was written; an
    /// existing file is then left exactly as it was.
    @discardableResult
    static func write(_ data: Data, to url: URL) -> Bool {
        let n = name(url)
        let (old, master) = lock.withLock { (kept[n] ?? opened[n], self.master) }
        let dek = old ?? SymmetricKey(size: .bits256)        // the same file keeps its key while this run knows it
        let id = UUID()
        do {
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            let header = Header(id: id, w: try LibraryKeys.wrapNew(dek, id: id, master: master))
            let blob = try join(header, try LibraryKeys.seal(data, with: dek))
            try blob.write(to: url, options: options)
        } catch {
            return false
        }
        lock.withLock { kept[n] = dek; opened[n] = nil }
        return true
    }

    /// Upgrades a file an older build wrote in the clear. The sealed copy replaces it in one atomic step, so the
    /// content is never missing. Returns true when the file is sealed afterwards. Unless `keepKey`, the key is not
    /// kept: after the upgrade the file opens like any other from before this run (after Face ID or the passcode).
    @discardableResult
    static func seal(inPlace url: URL, keepKey: Bool = false) -> Bool {
        if isSealed(url) { return true }
        guard let plain = try? Data(contentsOf: url), !plain.isEmpty else { return false }
        guard write(plain, to: url) else { return false }
        if !keepKey { lock.withLock { kept[name(url)] = nil } }
        return true
    }

    /// Keeps a file's key for the rest of this run (a conversation the owner opened and is now working in), so it
    /// stays readable after the Library locks. Only works while the file can be read.
    static func keep(_ url: URL) {
        guard let header = header(of: url), let dek = key(for: url, header: header) else { return }
        lock.withLock { kept[name(url)] = dek }
    }

    /// `keep` for every file in a folder (not its subfolders).
    static func keep(folder: URL) {
        for url in (try? FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil)) ?? [] {
            keep(url)
        }
    }

    /// Call after deleting a file, so its key doesn't stay in memory.
    static func forget(_ url: URL) {
        lock.withLock { kept[name(url)] = nil; opened[name(url)] = nil }
    }

    // MARK: decrypted temporary copies (a player needs a real file)

    /// A decrypted copy in the temporary folder, for a video or audio player. Removed on lock, at launch, and by
    /// `removeTempCopies`. nil when the file can't be read right now.
    static func tempCopy(of url: URL) -> URL? {
        let out = FileManager.default.temporaryDirectory
            .appendingPathComponent(tempPrefix + url.deletingPathExtension().lastPathComponent)
            .appendingPathExtension(url.pathExtension)
        if FileManager.default.fileExists(atPath: out.path) { return out }
        guard let data = read(url) else { return nil }
        return (try? data.write(to: out, options: [.atomic, .completeFileProtection])) != nil ? out : nil
    }

    static func removeTempCopies() {
        let tmp = FileManager.default.temporaryDirectory
        for url in (try? FileManager.default.contentsOfDirectory(at: tmp, includingPropertiesForKeys: nil)) ?? []
        where url.lastPathComponent.hasPrefix(tempPrefix) {
            try? FileManager.default.removeItem(at: url)
        }
    }
}
