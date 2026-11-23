import AVFoundation
import CryptoKit
import LocalAuthentication
import SwiftUI
import UIKit

/// The Library: every picture and video Fixer makes, kept sealed on this iPhone. It opens with Face ID or the
/// passcode (once, not once per item) and locks again when the app leaves the screen. Adding never needs the owner,
/// so the chat and Make keep saving while it is locked.
@MainActor
final class LibraryStore: ObservableObject {
    static let shared = LibraryStore()

    enum State: Equatable { case locked, unlocking, unlocked, unavailable(String) }

    @Published private(set) var state: State = .locked
    @Published private(set) var entries: [LibraryEntry] = []
    /// What is on disk, from file names alone (known even while locked).
    @Published private(set) var counts = LibraryVault.inventory()
    @Published var message: String?

    private var master: SymmetricKey?
    private var context: LAContext?
    private var deks: [UUID: SymmetricKey] = [:]
    private var writer: Task<Void, Never>?
    private var arrivedWhileUnlocking: [LibraryVault.Stored] = []   // saved after the listing started: merged when it ends
    private var epoch = 0                        // bumped by lock(), so an unlock that is still working gives up
    private var suppressAuto = false              // the owner said no to the prompt: don't ask again until the app has left
    private let thumbs = NSCache<NSUUID, UIImage>()

    private init() {}

    // MARK: adding (works while locked)

    /// Saves a picture to the Library and returns its id at once; the sealing happens in the background.
    @discardableResult
    func add(picture: UIImage, meta: LibraryMeta) -> UUID {
        let master = self.master
        enqueue { await Task.detached(priority: .utility) { LibraryVault.storePicture(picture, meta: meta, master: master) }.value }
        return meta.id
    }

    /// Saves a video. `source` is the video file already on disk (for the first-frame thumbnail).
    @discardableResult
    func addVideo(_ data: Data, meta: LibraryMeta, source: URL?) -> UUID {
        let master = self.master
        enqueue {
            // No file to read the first frame from (a chat video: its own copy is sealed): a short-lived private copy,
            // written and removed off the main thread.
            var scratch: URL?
            if source == nil {
                scratch = await Task.detached(priority: .utility) { () -> URL? in
                    let url = FileManager.default.temporaryDirectory
                        .appendingPathComponent("lib-src-\(meta.id.uuidString).mp4")
                    return (try? data.write(to: url, options: [.atomic, .completeFileProtection])) != nil ? url : nil
                }.value
            }
            let frame = await Self.firstFrame(source ?? scratch)
            if let scratch { try? FileManager.default.removeItem(at: scratch) }
            return await Task.detached(priority: .utility) {
                LibraryVault.storeVideo(data, meta: meta, thumbnail: frame, master: master)
            }.value
        }
        return meta.id
    }

    /// One at a time and in order, so `flush()` can wait for everything still being written.
    private func enqueue(_ work: @escaping () async -> LibraryVault.Stored?) {
        let previous = writer
        writer = Task {
            await previous?.value
            guard let stored = await work() else { return }
            self.didStore(stored)
        }
    }

    private func didStore(_ s: LibraryVault.Stored) {
        counts = s.inventory
        if state == .unlocking { arrivedWhileUnlocking.append(s); return }   // unlock's listing may have missed it
        guard master != nil, state == .unlocked else { return }       // locked: it shows up at the next unlock
        deks[s.meta.id] = s.dek
        insert(LibraryEntry(id: s.meta.id, meta: s.meta, isProtected: false))
    }

    private func insert(_ e: LibraryEntry) {
        entries.removeAll { $0.id == e.id }
        entries.append(e)
        entries.sort { $0.meta.created > $1.meta.created }
    }

    /// Waits until everything handed to `add` has been written.
    func flush() async { await writer?.value }

    nonisolated private static func firstFrame(_ url: URL?) async -> UIImage? {
        guard let url else { return nil }
        let generator = AVAssetImageGenerator(asset: AVURLAsset(url: url))
        generator.appliesPreferredTrackTransform = true
        generator.maximumSize = CGSize(width: 640, height: 640)
        guard let frame = try? await generator.image(at: .zero).image else { return nil }
        return UIImage(cgImage: frame)
    }

    // MARK: lock and unlock

    func unlock(automatic: Bool = false) async {
        if automatic && suppressAuto { return }
        switch state {
        case .locked, .unavailable: break          // unavailable is tried again (a passcode may have been set since)
        case .unlocking, .unlocked: return
        }
        state = .unlocking
        message = nil
        arrivedWhileUnlocking = []
        let mine = epoch
        let ctx = LAContext()
        var probe: NSError?
        guard ctx.canEvaluatePolicy(.deviceOwnerAuthentication, error: &probe) else {
            if probe?.code == LAError.passcodeNotSet.rawValue {
                state = .unavailable("Set a passcode in iOS Settings to open the Library. Your pictures and videos are still being saved.")
            } else {
                state = .locked
                message = "This iPhone can't check who you are right now."
            }
            return
        }
        do {
            _ = try await ctx.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: "Open your Library")
        } catch {
            guard mine == epoch else { return }
            state = .locked
            let code = (error as? LAError)?.code
            if code == .userCancel || code == .appCancel || code == .systemCancel { suppressAuto = true }
            else { message = "Couldn't unlock the Library." }
            return
        }
        guard mine == epoch else { ctx.invalidate(); return }
        do {
            let keys = try LibraryKeys.loadOrCreateMaster(context: ctx)
            await flush()                                          // what the chat saved while it was locked
            let master = keys.master
            let priv = keys.priv
            let loaded = await Task.detached(priority: .userInitiated) { LibraryVault.openAll(master: master, priv: priv) }.value
            guard mine == epoch else { ctx.invalidate(); return }
            self.master = master
            context = ctx
            SealedFiles.unlocked(master: master, priv: priv)   // conversations and songs open with the same unlock
            deks = Dictionary(loaded.map { ($0.meta.id, $0.dek) }, uniquingKeysWith: { a, _ in a })
            entries = loaded.map { LibraryEntry(id: $0.meta.id, meta: $0.meta, isProtected: $0.isProtected) }
                .sorted { $0.meta.created > $1.meta.created }
            for s in arrivedWhileUnlocking {              // saved while this was opening: add what the listing missed
                deks[s.meta.id] = s.dek
                insert(LibraryEntry(id: s.meta.id, meta: s.meta, isProtected: false))
            }
            arrivedWhileUnlocking = []
            counts = LibraryVault.inventory()
            state = .unlocked
        } catch {
            ctx.invalidate()
            guard mine == epoch else { return }
            state = .locked
            message = "Couldn't unlock the Library."
        }
    }

    /// Drops every key and every decoded picture, and deletes the temporary video copies.
    func lock() {
        epoch += 1
        master = nil
        SealedFiles.lockNow()
        deks = [:]
        entries = []
        arrivedWhileUnlocking = []
        thumbs.removeAllObjects()
        context?.invalidate()
        context = nil
        suppressAuto = false
        message = nil
        if case .unavailable = state {} else { state = .locked }
        LibraryVault.removeTempVideos()
    }

    // MARK: reading (while unlocked)

    func thumbnail(for entry: LibraryEntry) -> UIImage? {
        if let hit = thumbs.object(forKey: entry.id as NSUUID) { return hit }
        guard let data = entry.meta.thumbnail, let image = UIImage(data: data) else { return nil }
        thumbs.setObject(image, forKey: entry.id as NSUUID)
        return image
    }

    func image(_ id: UUID) async -> UIImage? {
        guard let dek = deks[id] else { return nil }
        return await Task.detached(priority: .userInitiated) {
            LibraryVault.read(id, kind: .picture, dek: dek).flatMap { UIImage(data: $0) }
        }.value
    }

    /// A playable copy of a video, decrypted into the temporary folder (removed on lock and when the viewer closes).
    func videoURL(_ id: UUID) async -> URL? {
        guard let dek = deks[id] else { return nil }
        let url = LibraryVault.tempVideoURL(id)
        let ok = await Task.detached(priority: .userInitiated) { () -> Bool in
            guard let data = LibraryVault.read(id, kind: .video, dek: dek) else { return false }
            return (try? data.write(to: url, options: [.atomic, .completeFileProtection])) != nil
        }.value
        return ok ? url : nil
    }

    func removeTempVideo(_ id: UUID) { try? FileManager.default.removeItem(at: LibraryVault.tempVideoURL(id)) }

    // MARK: protect and delete

    /// Protect works any time. Unprotect needs the Library open; returns false when it can't be done.
    @discardableResult
    func setProtected(_ id: UUID, _ on: Bool) -> Bool {
        if !on, state != .unlocked { return false }
        LibraryVault.setProtected(id, on)
        if let i = entries.firstIndex(where: { $0.id == id }) { entries[i].isProtected = on }
        counts = LibraryVault.inventory()
        return true
    }

    func delete(_ id: UUID) {
        LibraryVault.delete(id)
        entries.removeAll { $0.id == id }
        arrivedWhileUnlocking.removeAll { $0.meta.id == id }
        deks[id] = nil
        thumbs.removeObject(forKey: id as NSUUID)
        removeTempVideo(id)
        counts = LibraryVault.inventory()
    }

    // MARK: clear everything

    /// Erases every item that isn't protected; returns how many went. Waits for saves still in flight first.
    func eraseUnprotected() async -> Int {
        await flush()
        let gone = await Task.detached(priority: .userInitiated) { () -> Int in
            let ids = LibraryVault.unprotectedIDs()
            for id in ids { LibraryVault.delete(id) }
            return ids.count
        }.value
        let ids = Set(entries.filter { !$0.isProtected }.map(\.id))
        entries.removeAll { ids.contains($0.id) }
        for id in ids { deks[id] = nil }
        counts = LibraryVault.inventory()
        return gone
    }
}
