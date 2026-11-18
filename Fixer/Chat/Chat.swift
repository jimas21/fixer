import Photos
import SwiftUI
import UIKit

struct Message: Identifiable, Codable, Equatable {
    enum Role: String, Codable { case me, fixer }
    var id = UUID()
    var role: Role
    var text: String
    var image: String? = nil      // file name inside the conversation's folder
    var video: String? = nil      // .mp4 file name inside the conversation's folder
    var date = Date()
    // Added later: all Optional, so conversations saved by older builds still load.
    var error: Bool? = nil        // a failed request: drawn as a red bubble
    var retry: String? = nil      // the message to send again when Retry is tapped
    var libraryID: UUID? = nil    // the Library copy of this picture (slice 3)
    var thought: String? = nil    // what the model reasoned before answering (Think on)
    var thoughtSeconds: Double? = nil
    var queueLink: Bool? = nil    // 11-17: the error came from a paused chat: show "See what's running"
    var enhanced: String? = nil   // 11-17: the prompt the computer wrote from my words ("Enhance my prompt"), shown under the picture
}

struct Conversation: Identifiable, Codable, Equatable {
    var id = UUID()
    var title = "New chat"
    var updated = Date()
    var messages: [Message] = []
}

/// Conversations saved on the phone: Application Support/Conversations/<id>/conversation.json + images.
enum History {
    static var root: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Conversations", isDirectory: true)
    }
    static func folder(_ id: UUID) -> URL { root.appendingPathComponent(id.uuidString, isDirectory: true) }

    static func list() -> [Conversation] {
        let dirs = (try? FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)) ?? []
        return dirs.compactMap { dir in
            guard let data = try? Data(contentsOf: dir.appendingPathComponent("conversation.json")) else { return nil }
            return try? JSONDecoder().decode(Conversation.self, from: data)
        }.sorted { $0.updated > $1.updated }
    }

    static func save(_ c: Conversation) {
        guard !c.messages.isEmpty else { return }
        let dir = folder(c.id)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        if let data = try? JSONEncoder().encode(c) {
            try? data.write(to: dir.appendingPathComponent("conversation.json"),
                            options: [.atomic, .completeFileProtectionUnlessOpen])
        }
    }

    /// One saved conversation by id (11-17: a result that arrives for a chat that isn't the open one goes into its file).
    static func load(_ id: UUID) -> Conversation? {
        guard let data = try? Data(contentsOf: folder(id).appendingPathComponent("conversation.json")) else { return nil }
        return try? JSONDecoder().decode(Conversation.self, from: data)
    }

    static func delete(_ id: UUID) { try? FileManager.default.removeItem(at: folder(id)) }
    static func deleteAll() { try? FileManager.default.removeItem(at: root) }

    static let pictureTypes: Set<String> = ["jpg", "jpeg", "png", "heic", "heif", "webp", "gif"]
    static let videoTypes: Set<String> = ["mp4", "mov", "m4v"]

    /// What Clear everything will delete: conversations, pictures and videos (made and added), and their size.
    struct Inventory: Equatable { var conversations = 0, pictures = 0, videos = 0, other = 0; var bytes: Int64 = 0
        var isEmpty: Bool { conversations + pictures + videos + other == 0 } }

    static func inventory() -> Inventory {
        var inv = Inventory()
        let fm = FileManager.default
        inv.conversations = ((try? fm.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)) ?? []).count
        let keys: [URLResourceKey] = [.isRegularFileKey, .fileSizeKey]
        guard let walk = fm.enumerator(at: root, includingPropertiesForKeys: keys) else { return inv }
        for case let url as URL in walk {
            guard let v = try? url.resourceValues(forKeys: Set(keys)), v.isRegularFile == true else { continue }
            inv.bytes += Int64(v.fileSize ?? 0)
            let ext = url.pathExtension.lowercased()
            if pictureTypes.contains(ext) { inv.pictures += 1 }
            else if videoTypes.contains(ext) { inv.videos += 1 }
            else if url.lastPathComponent != "conversation.json" { inv.other += 1 }
        }
        return inv
    }

    /// Deletes every file with one of `types` in every conversation; returns how many went.
    @discardableResult
    static func deleteFiles(_ types: Set<String>) -> Int {
        let fm = FileManager.default
        guard let walk = fm.enumerator(at: root, includingPropertiesForKeys: nil) else { return 0 }
        var n = 0
        for case let url as URL in walk where types.contains(url.pathExtension.lowercased()) {
            if (try? fm.removeItem(at: url)) != nil { n += 1 }
        }
        return n
    }

    /// Temporary files left (picked photos, shared videos), not counting a model download in progress.
    static var scratchLeft: Int {
        let fm = FileManager.default
        return ((try? fm.contentsOfDirectory(at: fm.temporaryDirectory, includingPropertiesForKeys: nil)) ?? [])
            .filter { !$0.lastPathComponent.hasPrefix("CFNetworkDownload") }.count
    }

    /// Everything else a conversation can leave behind on the phone: temporary files (picked photos, shared videos),
    /// the URL cache, and iOS's app-switcher screenshots of the last screen. Leaves models, settings and any model
    /// download in progress alone. Photos already saved to the Photos library are the owner's and aren't touched.
    static func wipeScratch() {
        let fm = FileManager.default
        let tmp = fm.temporaryDirectory
        for item in (try? fm.contentsOfDirectory(at: tmp, includingPropertiesForKeys: nil)) ?? []
        where !item.lastPathComponent.hasPrefix("CFNetworkDownload") {
            try? fm.removeItem(at: item)
        }
        URLCache.shared.removeAllCachedResponses()
        let library = fm.urls(for: .libraryDirectory, in: .userDomainMask)[0]
        try? fm.removeItem(at: library.appendingPathComponent("SplashBoard", isDirectory: true))
    }

    /// True when nothing of any conversation is left on disk.
    static var isEmpty: Bool {
        ((try? FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)) ?? []).isEmpty
    }

    static func store(_ image: UIImage, in id: UUID) -> String? {
        let dir = folder(id)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let alpha = image.cgImage.map { ![.none, .noneSkipLast, .noneSkipFirst].contains($0.alphaInfo) } ?? false
        let name = UUID().uuidString + (alpha ? ".png" : ".jpg")
        guard let data = alpha ? image.pngData() : image.jpegData(compressionQuality: 0.92) else { return nil }
        do { try data.write(to: dir.appendingPathComponent(name), options: .completeFileProtectionUnlessOpen); return name }
        catch { return nil }
    }

    static func storeVideo(_ data: Data, in id: UUID) -> String? {
        let dir = folder(id)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let name = UUID().uuidString + ".mp4"
        do { try data.write(to: dir.appendingPathComponent(name), options: .completeFileProtectionUnlessOpen); return name }
        catch { return nil }
    }

    static func videoURL(_ name: String, in id: UUID) -> URL { folder(id).appendingPathComponent(name) }

    static func image(_ name: String, in id: UUID) -> UIImage? {
        UIImage(contentsOfFile: folder(id).appendingPathComponent(name).path)
    }
}

/// The one conversation on screen: messages, the photo being worked on (with paint mask and undo), and the brain.
@MainActor
final class Chat: ObservableObject {
    @Published private(set) var conversation = Conversation()
    @Published private(set) var photo: UIImage?
    @Published private(set) var undoStack: [UIImage] = []
    @Published var strokes: [Stroke] = [] { didSet { refreshMask() } }
    @Published var pickedMask: CGImage? { didSet { refreshMask() } }
    @Published private(set) var maskOverlay: UIImage?
    @Published var busy: String?
    @Published var progress: Double?
    @Published var notice: String?
    @Published var pendingPlan: Plan?
    // Counters that drive haptics (each bump is one buzz).
    @Published private(set) var doneCount = 0
    @Published private(set) var errorCount = 0
    @Published private(set) var sendCount = 0

    private(set) var mask: CGImage?
    private var photoUnseen = false        // the brain hasn't been shown the current photo yet
    private var photoLibraryID: UUID?      // the Library copy of the photo being worked on (the parent of what comes next)
    private var task: Task<Void, Never>?
    private var thinkingLabel = "Thinking…"    // "Writing the prompt…" while a picture's prompt is being written

    /// Computer work this chat started (11-17): runs in the background, so the conversation stays usable meanwhile.
    struct Job: Identifiable, Equatable {
        let id = UUID()
        let conversation: UUID
        let slot: String
        var title: String
        var status = "Starting…"
        var progress: Double?
        var pcID: String?
        var base: UIImage? = nil            // the photo it works on (nil for a new picture)
        var preview: UIImage? = nil         // the half-drawn picture, when the computer sends one
    }
    @Published private(set) var jobs: [Job] = []
    @Published private(set) var phoneWork = false       // the phone itself is drawing (computer unreachable)
    private var jobTasks: [UUID: Task<Void, Never>] = [:]
    /// Thinking has finished and the phone isn't drawing: a new message may go.
    var canSend: Bool { busy == nil && !phoneWork }

    init() {
        JobInbox.shared.toChat = { [weak self] conv, kind, words, frames in
            Task { await self?.adopt(conv, kind: kind, words: words, frames: frames) }
        }
    }

    var cgImage: CGImage? { photo.flatMap(ImageKit.cgImage) }
    var messages: [Message] { conversation.messages }

    // MARK: conversations

    func newChat() {
        stop()
        save()
        conversation = Conversation()
        photo = nil
        photoLibraryID = nil
        undoStack = []
        clearMask()
        Brain.shared.resetChat()
    }

    func open(_ c: Conversation) {
        stop()
        save()
        conversation = c
        undoStack = []
        clearMask()
        photo = c.messages.last(where: { $0.image != nil }).flatMap { m in m.image.flatMap { History.image($0, in: c.id) } }
        photoUnseen = photo != nil
        photoLibraryID = c.messages.last(where: { $0.image != nil })?.libraryID
        Brain.shared.resetChat()
    }

    /// Clear everything, step 1: stops any running job and drops the conversation, photo, mask and undo history held
    /// in memory (replaced before the files go, so nothing saves the old conversation back). The other steps are
    /// run one by one by ClearEverythingView so each can be shown as it finishes.
    func wipeSession() {
        stop()
        conversation = Conversation()
        photo = nil
        photoLibraryID = nil
        undoStack = []
        clearMask()
        pendingPlan = nil
        photoUnseen = false
        notice = nil
        for t in jobTasks.values { t.cancel() }          // the computer's side is erased by "forget" in Clear everything
        jobTasks = [:]
        jobs = []
        phoneWork = false
    }

    func save() {
        guard !conversation.messages.isEmpty else { return }
        conversation.updated = Date()
        if conversation.title == "New chat",
           let first = conversation.messages.first(where: { $0.role == .me && !$0.text.isEmpty }) {
            conversation.title = String(first.text.prefix(48))
        }
        History.save(conversation)
    }

    @discardableResult
    func append(_ m: Message) -> UUID {
        // A job stopped by "new chat" or "clear everything" can still finish (a PC video, an error message);
        // its result must not land in the next conversation.
        guard !Task.isCancelled else { return m.id }
        conversation.messages.append(m)
        return m.id
    }

    func say(_ text: String) { append(Message(role: .fixer, text: text)) }

    /// A failed request shows as a red bubble in the conversation, with a Retry button when there is text to resend.
    func fail(_ error: Error, retry: String?, in conv: UUID? = nil) {
        guard !Task.isCancelled else { return }
        var m = Message(role: .fixer, text: error.localizedDescription)
        m.error = true
        m.retry = retry
        if let p = error as? PCError, case .paused = p { m.queueLink = true }
        if let conv, conv != conversation.id { post(m, to: conv) } else { append(m) }
        errorCount += 1
    }

    /// A message into conversation `conv`: the open one in memory, any other straight into its saved file.
    private func post(_ m: Message, to conv: UUID) {
        if conv == conversation.id { append(m); save(); return }
        guard var c = History.load(conv) else { return }        // deleted meanwhile: dropped
        c.messages.append(m)
        c.updated = Date()
        History.save(c)
    }

    /// Sends a failed message again without repeating it in the conversation.
    func retry(_ id: UUID) {
        guard canSend, let i = conversation.messages.firstIndex(where: { $0.id == id }),
              let text = conversation.messages[i].retry else { return }
        conversation.messages.remove(at: i)
        send(text, attachment: nil, isRetry: true)
    }

    private func dropIfEmpty(_ id: UUID) {
        if let i = conversation.messages.firstIndex(where: { $0.id == id }),
           conversation.messages[i].text.isEmpty || conversation.messages[i].text == "…" {
            conversation.messages.remove(at: i)
        }
    }

    private func update(_ id: UUID, text: String) {
        if let i = conversation.messages.firstIndex(where: { $0.id == id }) { conversation.messages[i].text = text }
    }

    private func setThought(_ id: UUID, _ text: String, seconds: Double) {
        if let i = conversation.messages.firstIndex(where: { $0.id == id }) {
            conversation.messages[i].thought = text
            conversation.messages[i].thoughtSeconds = seconds
        }
    }

    /// Short text recap, used when the brain's memory had to be rebuilt mid-conversation.
    private func recap() -> String {
        conversation.messages.suffix(10).filter { !$0.text.isEmpty }
            .map { ($0.role == .me ? "User: " : "Fixer: ") + String($0.text.prefix(400)) }
            .joined(separator: "\n")
    }

    /// The conversation so far (before the message just sent), for the PC brain.
    /// The conversation so far, newest kept first, as much as fits the computer's model (its memory holds 16k tokens;
    /// ~36k characters leaves room for the instructions and the reply). 11-17: it was the last 30 messages whatever
    /// their size, error bubbles were sent as Fixer's words, and pictures without text were left out entirely.
    private func pcHistory() -> [(role: String, text: String)] {
        var out: [(role: String, text: String)] = []
        var budget = Int(RemoteConfig.string("history_chars") ?? "") ?? 36_000
        for m in conversation.messages.dropLast().reversed() where m.error != true {
            var t = m.text.trimmingCharacters(in: .whitespacesAndNewlines)
            if t == "…" { t = "" }
            let media = m.video != nil ? "a video" : m.image != nil ? (m.role == .me ? "a photo" : "a picture") : nil
            if let media { t = t.isEmpty ? "(\(media))" : "(\(media)) \(t)" }
            if t.isEmpty { continue }
            if t.count > budget { break }
            budget -= t.count
            out.append((role: m.role == .me ? "user" : "assistant", text: t))
        }
        return out.reversed()
    }

    // MARK: photo

    func closePhoto() { photo = nil; photoLibraryID = nil; undoStack = []; clearMask() }

    func clearMask() { strokes = []; pickedMask = nil }

    /// A tool's result: becomes the current photo and appears in the conversation.
    /// `made` says how it was made; the picture is then also kept in the Library. `libraryID` is for a picture that is
    /// already in the Library (sent over from Make), so it isn't saved twice.
    func commit(_ cg: CGImage, caption: String, made: Made? = nil, libraryID: UUID? = nil, into conv: UUID? = nil) {
        guard !Task.isCancelled else { return }        // stopped job: nothing is kept
        if let conv, conv != conversation.id {
            // 11-17: the result of work started in another chat lands in that chat, not in the one on screen.
            guard History.load(conv) != nil else { return }
            let ui = UIImage(cgImage: cg)
            var m = Message(role: .fixer, text: caption, image: History.store(ui, in: conv))
            m.enhanced = made?.enhanced
            if let made {
                m.libraryID = LibraryStore.shared.add(picture: ui, meta: made.meta(kind: .picture, conversation: conv))
            } else {
                m.libraryID = libraryID
            }
            post(m, to: conv)
            doneCount += 1
            return
        }
        if let photo { undoStack.append(photo) }
        if undoStack.count > 12 { undoStack.removeFirst() }
        let ui = UIImage(cgImage: cg)
        photo = ui
        clearMask()
        photoUnseen = true
        var m = Message(role: .fixer, text: caption, image: History.store(ui, in: conversation.id))
        m.enhanced = made?.enhanced
        if let made {
            var info = made
            info.parent = info.parent ?? photoLibraryID
            m.libraryID = LibraryStore.shared.add(picture: ui, meta: info.meta(kind: .picture, conversation: conversation.id))
        } else {
            m.libraryID = libraryID
        }
        photoLibraryID = m.libraryID
        append(m)
        doneCount += 1
        save()
    }

    /// A video result: appears in the conversation (it doesn't replace the photo being worked on).
    func commitVideo(_ data: Data, made: Made? = nil, into conv: UUID? = nil) {
        guard !Task.isCancelled else { return }        // stopped job: nothing is kept
        if let conv, conv != conversation.id {
            guard History.load(conv) != nil else { return }
            let name = History.storeVideo(data, in: conv)
            var m = Message(role: .fixer, text: name == nil ? "Couldn't save the video." : "", video: name)
            if let made {
                m.libraryID = LibraryStore.shared.addVideo(data, meta: made.meta(kind: .video, conversation: conv),
                                                           source: name.map { History.videoURL($0, in: conv) })
            }
            post(m, to: conv)
            doneCount += 1
            return
        }
        let name = History.storeVideo(data, in: conversation.id)
        var m = Message(role: .fixer, text: name == nil ? "Couldn't save the video." : "", video: name)
        if let made {
            var info = made
            info.parent = info.parent ?? photoLibraryID
            m.libraryID = LibraryStore.shared.addVideo(data, meta: info.meta(kind: .video, conversation: conversation.id),
                                                       source: name.map { History.videoURL($0, in: conversation.id) })
        }
        append(m)
        doneCount += 1
        save()
    }

    func undo() {
        guard let last = undoStack.popLast() else { say("Nothing to undo."); return }
        photo = last
        photoLibraryID = nil
        clearMask()
        photoUnseen = true
        append(Message(role: .fixer, text: "Went back to the previous version.", image: History.store(last, in: conversation.id)))
        save()
    }

    func usePhoto(_ m: Message) {
        guard let name = m.image, let ui = History.image(name, in: conversation.id) else { return }
        if let photo { undoStack.append(photo) }
        photo = ui
        photoLibraryID = m.libraryID
        clearMask()
        photoUnseen = true
    }

    /// Runs one action on a picture picked in the viewer: that picture becomes the one being worked on (unless it
    /// already is), and the painted spot, if any, is the mask.
    func apply(_ action: Plan.Action, to messageID: UUID, mask: CGImage? = nil) {
        guard canSend else { return }
        if let m = conversation.messages.first(where: { $0.id == messageID }), m.image != nil {
            let latest = conversation.messages.last(where: { $0.image != nil })?.id
            if latest != messageID || photo == nil { usePhoto(m) }
        }
        strokes = []
        pickedMask = mask
        run(Plan(say: "", actions: [action]))
    }

    private func refreshMask() {
        guard let photo else { mask = nil; maskOverlay = nil; return }
        let painted = BrushMask.render(strokes, size: CGSize(width: photo.size.width * photo.scale,
                                                              height: photo.size.height * photo.scale))
        mask = ImageKit.union(painted, pickedMask)
        maskOverlay = mask.flatMap(ImageKit.overlay)
    }

    // MARK: computer work (11-17)

    /// Starts a plan's picture/video work on the computer as a background job: the conversation stays usable meanwhile.
    func startJob(_ plan: Plan) {
        let conv = conversation.id
        let slot = plan.lineUp ? "chat:\(conv.uuidString):\(UUID().uuidString.prefix(8))" : "chat:\(conv.uuidString)"
        if !plan.lineUp {                                   // same place: the computer replaces it; drop our watcher
            for j in jobs where j.slot == slot { jobTasks[j.id]?.cancel(); jobTasks[j.id] = nil }
            jobs.removeAll { $0.slot == slot }
        }
        var made = Job(conversation: conv, slot: slot, title: Agent.title(plan))
        if let first = plan.actions.first(where: { !$0.isQueueControl }) {
            switch first {
            case .redraw, .remove, .cutout, .upscale: made.base = photo
            case .video(_, let fromPhoto): if fromPhoto { made.base = photo }
            case .make, .search, .queueCancel, .queueMove, .stopAll, .freeMemory: break
            }
        }
        let job = made
        jobs.append(job)
        let ctx = JobContext(id: job.id, conversation: conv, slot: slot)
        jobTasks[job.id] = Task {
            do { try await Agent.run(plan, chat: self, job: ctx) }
            catch is CancellationError {}
            catch PCError.stopped(_) {}                     // replaced or stopped on the computer: nothing to show
            catch { fail(error, retry: nil, in: conv) }
            endJob(job.id)
        }
    }

    /// Sets only the values given (status lines, progress, the computer's job id once it says "queued").
    func updateJob(_ id: UUID, status: String? = nil, progress: Double? = nil, pcID: String? = nil,
                   preview: UIImage? = nil) {
        guard let i = jobs.firstIndex(where: { $0.id == id }) else { return }
        if let preview { jobs[i].preview = preview }
        if let status { jobs[i].status = status }
        if let progress { jobs[i].progress = progress }
        if let pcID { jobs[i].pcID = pcID }
    }

    /// The job bubble's Stop: this job only (11-17: Stop used to stop everything on the computer).
    func cancelJob(_ id: UUID) {
        guard let j = jobs.first(where: { $0.id == id }) else { return }
        jobTasks[id]?.cancel()
        jobTasks[id] = nil
        jobs.removeAll { $0.id == id }
        if let pc = j.pcID { Task { _ = try? await Desktop.shared.cancelJobs([pc]) } }
        else { Desktop.shared.cancel(slot: j.slot) }        // not in line yet (or an old computer): by place
    }

    /// The composer's Stop when only computer work is running: every job of the open conversation, nothing else.
    func stopThisChat() {
        let conv = conversation.id
        for j in jobs where j.conversation == conv { cancelJob(j.id) }
        if phoneWork { stop() }
    }

    private func endJob(_ id: UUID) {
        jobTasks[id] = nil
        jobs.removeAll { $0.id == id }
    }

    /// The phone's own drawing (the computer can't be reached): waits until the chat has finished thinking, and keeps
    /// the chat from starting the phone brain meanwhile.
    func onPhone<T>(_ work: () async throws -> T) async throws -> T {
        while busy != nil { try await Task.sleep(nanoseconds: 300_000_000) }
        phoneWork = true
        defer { phoneWork = false }
        return try await work()
    }

    /// Finished work the computer held while Fixer was closed, filed into the chat that asked for it (11-17).
    func adopt(_ conv: UUID, kind: String, words: String, frames: [[String: Any]]) async {
        let rules = Rules.shared
        let filter = (kind == "edit" || kind == "change" || kind == "upscale") ? rules.editFilter : rules.newFilter
        for f in frames {
            guard let b64 = f["v"] as? String, let data = Data(base64Encoded: b64) else { continue }
            let note = words.isEmpty ? nil : words
            switch f["t"] as? String {
            case "image":
                guard let cg = UIImage(data: data)?.cgImage, !Detector.blocks(cg, filter) else { continue }
                let action = (kind == "edit" || kind == "change") ? "edit" : kind == "upscale" ? "upscale" : "make"
                commit(cg, caption: "", made: Made(action: action, words: note), into: conv)
            case "video":
                if await ContentGate.blocksVideo(data, filter) { continue }
                commitVideo(data, made: Made(action: "video", words: note), into: conv)
            default: break
            }
        }
    }

    // MARK: talking

    func stop() {
        task?.cancel()
        task = nil
        busy = nil
        progress = nil
    }

    func send(_ raw: String, attachment: UIImage?, isRetry: Bool = false) {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard (!text.isEmpty || attachment != nil), canSend else { return }
        sendCount += 1
        var msg = Message(role: .me, text: text)
        if let attachment {
            let up = ImageKit.upright(attachment)
            if let photo { undoStack.append(photo) }
            photo = up
            clearMask()
            photoUnseen = true
            msg.image = History.store(up, in: conversation.id)
            // A photo the owner adds is kept in the Library too, so it can be found and used again.
            msg.libraryID = LibraryStore.shared.add(picture: up, meta: Made(source: .added, action: "added")
                .meta(kind: .picture, conversation: conversation.id))
            photoLibraryID = msg.libraryID
        }
        if !isRetry { append(msg) }
        guard !text.isEmpty else { save(); return }
        if ["undo", "undo that", "undo it", "go back"].contains(text.lowercased()) { undo(); return }

        let painted = mask != nil
        let pc = Desktop.shared.isOn
        // The PC keeps nothing between turns, so it gets the conversation and the current photo every time.
        let image = pc ? cgImage : (photoUnseen ? cgImage : nil)
        let history = pc ? pcHistory() : []
        let recapText = recap()
        let replyID = append(Message(role: .fixer, text: ""))
        busy = pc ? "Thinking…" : "Thinking…"
        task = Task {
            do {
                // A normal assistant by default; the photo tools ride along only on photo messages.
                let photoTurn = attachment != nil || painted || Plan.asksForPicture(text)
                    || Plan.asksForAnother(text, after: conversation.messages)
                    || image != nil      // a photo is open: the brain decides change vs. chat, no word list (11-17)
                thinkingLabel = photoTurn ? "Writing the prompt…" : "Thinking…"
                busy = thinkingLabel
                let prompt = (photoTurn ? Brain.photoTools + "\n\nMy message: " : "") + text
                    + (painted ? "\n(I painted an area of the photo.)" : "")
                    + (pc && image != nil ? "\n(This is the current photo.)" : "")
                var full = try await think(prompt, image: image, history: history, recap: recapText, pc: pc, into: replyID)
                if image != nil, !pc { photoUnseen = false }
                var plan = Plan.parse(full)
                // A search nobody needed ("Test" went to the web, 11-17): answer directly instead.
                if plan.searchQuery != nil, !Plan.mayNeedWeb(text) {
                    busy = "Thinking…"
                    full = try await think("Answer my last message directly. Do not search the web.", image: nil,
                                           history: pc ? pcHistory() : [], recap: "", pc: pc, into: replyID)
                    plan = Plan.parse(full)
                    plan.actions.removeAll { if case .search = $0 { return true } else { return false } }
                    if plan.say.isEmpty, plan.actions.isEmpty { plan.say = full.trimmingCharacters(in: .whitespacesAndNewlines) }
                }
                // Web search: run it, hand the results back, let the brain answer (at most two rounds).
                var rounds = 0
                while let query = plan.searchQuery, rounds < 2 {
                    rounds += 1
                    update(replyID, text: "Searching the web for “\(query)”…")
                    busy = "Searching…"
                    let results = try await WebSearch.run(query)
                    busy = pc ? "Thinking…" : "Thinking…"
                    let followUp = "Web results for \"\(query)\":\n\(results)\n\nUse these to answer my last message. Mention the sources you used."
                    full = try await think(followUp, image: nil, history: pc ? pcHistory() : [], recap: "", pc: pc, into: replyID)
                    plan = Plan.parse(full)
                }
                plan.actions.removeAll { if case .search = $0 { return true } else { return false } }
                // A brain that "answers" a plain question by making an image (it drew the 50 state capitals as
                // gibberish once): ask again for text instead of starting the image generator.
                let worked = plan.actions.filter { !$0.isQueueControl }
                let makesOnly = !worked.isEmpty && worked.allSatisfy { if case .make = $0 { return true } else { return false } }
                if makesOnly, !Plan.asksForPicture(text), !Plan.asksForAnother(text, after: conversation.messages) {
                    busy = pc ? "Thinking…" : "Thinking…"
                    let retry = "Answer my last message in plain text. Do not make an image."
                    full = try await think(retry, image: nil, history: pc ? pcHistory() : [], recap: "", pc: pc, into: replyID)
                    plan = Plan.parse(full)
                    plan.actions.removeAll { if case .make = $0 { return true } else { return false } }
                    if plan.say.isEmpty, plan.actions.isEmpty { plan.say = full.trimmingCharacters(in: .whitespacesAndNewlines) }
                }
                plan = plan.withPrompts(userText: text, useMyWords: Rules.shared.useMyWords)
                update(replyID, text: plan.say.isEmpty ? (plan.actions.isEmpty ? "…" : "On it.") : plan.say)
                // 11-17: managing the computer's line (stop, move, free memory) runs at once, before any picture work.
                let control = plan.actions.filter { $0.isQueueControl }
                var work = plan
                work.actions.removeAll { $0.isQueueControl }
                if !control.isEmpty { await Agent.control(control, chat: self, said: !plan.say.isEmpty) }
                if !work.actions.isEmpty {
                    // Photo changes just run. Only a video (minutes of the computer's time) asks first.
                    let hasVideo = work.actions.contains { if case .video = $0 { return true } else { return false } }
                    if Rules.shared.askBeforeVideo, hasVideo {
                        pendingPlan = work
                    } else if pc {
                        startJob(work)                      // the computer works, the chat stays free
                    } else {
                        try await Agent.run(work, chat: self)   // on the phone: one thing at a time, as before
                    }
                }
            } catch is CancellationError {
            } catch {
                dropIfEmpty(replyID)
                fail(error, retry: text)
            }
            busy = nil
            progress = nil
            save()
        }
    }

    /// One turn, streamed into the reply message. Uses the computer when Fixer Desktop is on; if it can't be
    /// reached, answers on this iPhone instead. Returns the full raw reply.
    private func think(_ prompt: String, image: CGImage?, history: [(role: String, text: String)], recap: String,
                       pc: Bool, into replyID: UUID) async throws -> String {
        if pc {
            do { return try await streamTurn(prompt, image: image, history: history, recap: recap, pc: true, into: replyID) }
            catch let e as PCError where e.isUnreachable {
                try Task.checkCancellation()           // stopped (new chat / clear everything): no phone fallback
                busy = "Your computer isn't reachable. Answering on this iPhone…"
            }
        }
        return try await streamTurn(prompt, image: image, history: history, recap: recap, pc: false, into: replyID)
    }

    private func streamTurn(_ prompt: String, image: CGImage?, history: [(role: String, text: String)], recap: String,
                            pc: Bool, into replyID: UUID) async throws -> String {
        let stream: AsyncThrowingStream<String, Error>
        if pc {
            let started = Date()
            var thought = ""
            stream = try Desktop.shared.chat(history: history, text: prompt, image: image,
                                             slot: "chat:\(conversation.id.uuidString)",
                                             status: { [weak self] s in self?.busy = s },
                                             thought: { [weak self] t in
                                                 thought += t
                                                 self?.setThought(replyID, thought, seconds: Date().timeIntervalSince(started))
                                             })
        } else {
            stream = try await Brain.shared.stream(prompt, image: image, photo: cgImage, recap: recap)
        }
        var full = ""
        for try await chunk in stream {
            if busy != thinkingLabel { busy = thinkingLabel }
            full += chunk
            update(replyID, text: Self.visible(full))
        }
        return full
    }

    /// Runs a plan directly (quick buttons, or after "ask before changing" is confirmed).
    func run(_ plan: Plan) {
        if Desktop.shared.isOn { startJob(plan); return }       // 11-17: the computer works, the chat stays free
        guard busy == nil else { return }
        busy = "Working…"
        task = Task {
            do { try await Agent.run(plan, chat: self) }
            catch is CancellationError {}
            catch { fail(error, retry: nil) }
            busy = nil
            progress = nil
            save()
        }
    }

    /// Hides raw action JSON while it streams.
    private static func visible(_ s: String) -> String {
        let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
        return t.hasPrefix("{") || t.hasPrefix("```") ? "…" : s
    }

    func saveToPhotos() {
        guard let image = photo else { return }
        PHPhotoLibrary.requestAuthorization(for: .addOnly) { status in
            Task { @MainActor in
                guard status == .authorized || status == .limited else {
                    self.notice = "To save photos, allow Fixer to add to your library in iOS Settings."
                    return
                }
                PHPhotoLibrary.shared().performChanges({
                    if let data = image.pngData() {
                        PHAssetCreationRequest.forAsset().addResource(with: .photo, data: data, options: nil)
                    }
                }) { ok, error in
                    Task { @MainActor in
                        self.notice = ok ? "Saved to Photos." : (error?.localizedDescription ?? "Couldn't save.")
                    }
                }
            }
        }
    }
}
