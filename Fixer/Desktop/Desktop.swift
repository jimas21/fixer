import CryptoKit
import Foundation
import Security
import UIKit

/// The link to the owner's PC (Fixer Desktop): addresses, this phone's id and its own 256-bit key.
/// Kept in the keychain (this device only). Made by scanning the QR code `pair.py` shows on the PC.
struct PCLink: Codable, Equatable {
    var home: String            // e.g. http://192.0.2.10:8765
    var away: String?           // e.g. https://fixer.example.org (Cloudflare), optional
    var id: String
    var key: String             // base64url
    var name: String

    static func from(url: URL) -> PCLink? {
        guard url.scheme == "fixer", url.host == "pair",
              let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems else { return nil }
        func q(_ k: String) -> String? { items.first { $0.name == k }?.value.flatMap { $0.isEmpty ? nil : $0 } }
        guard let home = q("u"), let id = q("id"), let key = q("k"), PCLink.decode(key)?.count == 32 else { return nil }
        return PCLink(home: home, away: q("r"), id: id, key: key, name: q("n") ?? "your computer")
    }

    static func decode(_ s: String) -> Data? {
        var b = s.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        b += String(repeating: "=", count: (4 - b.count % 4) % 4)
        return Data(base64Encoded: b)
    }

    var symmetricKey: SymmetricKey? { PCLink.decode(key).map { SymmetricKey(data: $0) } }

    // MARK: keychain

    private static let account = "pc-link"
    private static let service = "org.immortaldual.fixer"

    static func load() -> PCLink? {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                                kSecAttrAccount as String: account, kSecReturnData as String: true]
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let data = out as? Data else { return nil }
        return try? JSONDecoder().decode(PCLink.self, from: data)
    }

    func save() {
        PCLink.forget()
        guard let data = try? JSONEncoder().encode(self) else { return }
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: PCLink.service,
                                kSecAttrAccount as String: PCLink.account, kSecValueData as String: data,
                                kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        SecItemAdd(q as CFDictionary, nil)
    }

    static func forget() {
        let q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                                kSecAttrAccount as String: account]
        SecItemDelete(q as CFDictionary)
    }
}

enum PCError: LocalizedError {
    case notPaired, unreachable(String), refused, server(String), bad
    case dropped                 // 11-17: the connection ended mid-reply (phone backgrounded, network change)
    case stopped(String)         // 11-17: the computer stopped this job (replaced, cancelled, stop all, cleared)
    case paused(String)          // 11-17: the computer paused the chat for a video
    case gone(String)            // 11-17: the computer no longer has this job
    var isUnreachable: Bool { if case .unreachable = self { return true } else { return false } }

    var errorDescription: String? {
        switch self {
        case .notPaired: "Fixer Desktop isn't connected. Connect it in Settings."
        case .unreachable: "Can't reach your computer. Check that Fixer Desktop is on."
        case .refused: "Your computer didn't accept this iPhone. Pair it again in Settings."
        case .server(let s): s
        case .bad: "Got an unreadable response from your computer. Try again."
        case .dropped: "Lost the connection to your computer. Try again."
        case .stopped(let s): s
        case .paused(let s): s
        case .gone(let s): s
        }
    }
}

/// Runs one piece of work at a time; callers arriving while it runs wait for that same run.
actor ForgetGate {
    private var running: Task<Void, Error>?

    func run(_ work: @escaping @Sendable () async throws -> Void) async throws {
        if let running { try await running.value; return }
        let t = Task { try await work() }
        running = t
        defer { running = nil }
        try await t.value
    }
}

/// Talks to Fixer Desktop. Every request and every reply frame is AES-GCM sealed with this phone's key;
/// the request's timestamp and the frame number are part of what's authenticated, so nothing can be
/// replayed, reordered or swapped. Nothing is cached: the URL session keeps no cookies, cache or credentials.
@MainActor
final class Desktop: ObservableObject {
    static let shared = Desktop()

    @Published private(set) var link: PCLink? = PCLink.load()

    private nonisolated static let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.urlCache = nil
        c.requestCachePolicy = .reloadIgnoringLocalCacheData
        c.timeoutIntervalForRequest = 600
        c.timeoutIntervalForResource = 3600
        c.waitsForConnectivity = false
        return URLSession(configuration: c)
    }()

    private init() {}

    var isPaired: Bool { link != nil }
    var isOn: Bool { isPaired && Rules.shared.usePC }

    /// Set by "clear everything" until the PC confirms it dropped its memory; a yes/no flag, no content.
    /// UserDefaults is thread-safe, so the request code reads it off the main actor.
    nonisolated static var forgetOwed: Bool {
        get { UserDefaults.standard.bool(forKey: "pc.forgetOwed") }
        set { UserDefaults.standard.set(newValue, forKey: "pc.forgetOwed") }
    }
    var forgetOwed: Bool {
        get { Self.forgetOwed }
        set { Self.forgetOwed = newValue }
    }

    /// One owed "forget" at a time: requests that start together wait for the same one instead of each sending
    /// their own (a second one arriving late would stop the chat model mid-answer).
    nonisolated static let forgetGate = ForgetGate()

    func pair(_ l: PCLink) { l.save(); link = l; Rules.shared.usePC = true }
    func unpair() { PCLink.forget(); link = nil; Rules.shared.usePC = false }

    /// 11-17: set once any reply frame reached the caller, so a drop after that is never re-sent to the other address
    /// (the computer already accepted the request; sending it again would replace itself and start over).
    final class Seen: @unchecked Sendable { var got = false }

    /// One call: yields the decrypted reply frames until "done" (errors throw).
    nonisolated func call(_ op: String, _ payload: [String: Any], link: PCLink) -> AsyncThrowingStream<[String: Any], Error> {
        let body = try? JSONSerialization.data(withJSONObject: payload)
        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    guard let key = link.symmetricKey else { throw PCError.notPaired }
                    guard let body else { throw PCError.bad }
                    var lastError: Error = PCError.unreachable("no address")
                    let seen = Seen()
                    for base in await Self.routes(link) {
                        do {
                            // A "clear everything" the PC hasn't confirmed yet goes first: the PC must not hold
                            // the old conversation while it serves anything new.
                            if op != "forget", Self.forgetOwed {
                                try await Self.forgetGate.run {
                                    guard Self.forgetOwed else { return }       // another request just delivered it
                                    let (_, sink) = AsyncThrowingStream<[String: Any], Error>.makeStream()
                                    do {
                                        try await Self.exchange(base: base, op: "forget", body: Data("{}".utf8),
                                                                key: key, link: link, into: sink)
                                    } catch let e as URLError {
                                        throw e                                 // not reached: stays owed, next address
                                    } catch PCError.refused {
                                        // The PC no longer knows this phone (unpaired): nothing of it to erase
                                        // there, and owing it forever would fail every request.
                                    }
                                    Self.forgetOwed = false
                                }
                            }
                            try await Self.exchange(base: base, op: op, body: body, key: key, link: link, into: continuation,
                                                    seen: seen)
                            continuation.finish()
                            return
                        } catch let e as URLError where e.code != .cancelled {
                            if seen.got { throw PCError.dropped }                   // 11-17: accepted already: never re-send
                            await Self.routeMemory.clear()                          // race again next time
                            lastError = PCError.unreachable(e.localizedDescription)   // try the next address
                        }
                    }
                    throw lastError
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    /// The order to try the addresses in. Both are knocked on at once and whichever answers first goes first, so
    /// away from home nothing waits on the home address timing out (11-17: replies from away started late). The
    /// winner is remembered for 30 s; a request that fails on it clears the memory and the next one races again.
    private nonisolated static let routeMemory = RouteMemory()

    /// 11-19: when NO address answers the knock, the computer is off or out of reach: return none, so the caller fails
    /// in seconds (and chat answers on the iPhone) instead of trying each address in turn with a 90 s timeout.
    nonisolated static func routes(_ link: PCLink) async -> [String] {
        let all = [link.home, link.away].compactMap { $0 }
        guard !all.isEmpty else { return all }
        if let best = await routeMemory.current(), all.contains(best) { return [best] + all.filter { $0 != best } }
        let winner: String? = await withTaskGroup(of: String?.self) { group in
            for base in all {
                group.addTask { await knock(base) ? base : nil }
            }
            for await r in group where r != nil { group.cancelAll(); return r }
            return nil
        }
        guard let winner else { return [] }
        await routeMemory.set(winner)
        return [winner] + all.filter { $0 != winner }
    }

    /// Any HTTP answer below 500 means the address is reachable (the content doesn't matter; nothing is sent).
    /// 11-19: a 5xx is Cloudflare saying the tunnel or the computer behind it is down, so it doesn't count.
    private nonisolated static func knock(_ base: String) async -> Bool {
        guard let url = URL(string: base.trimmingCharacters(in: CharacterSet(charactersIn: "/")) + "/") else { return false }
        var req = URLRequest(url: url, timeoutInterval: 6)
        req.httpMethod = "HEAD"
        do {
            let (_, response) = try await session.data(for: req)
            return ((response as? HTTPURLResponse)?.statusCode ?? 0) < 500
        } catch { return false }
    }

    /// 11-19: HTTP codes that mean "the computer isn't there" (Cloudflare's own 52x pages, gateway errors), handled
    /// like a failed connection: try the other address, then the iPhone answers. Fixer itself never sends these.
    private nonisolated static func meansUnreachable(_ code: Int) -> Bool { (502...504).contains(code) || (520...530).contains(code) }

    private nonisolated static func exchange(base: String, op: String, body: Data, key: SymmetricKey, link: PCLink,
                                             into out: AsyncThrowingStream<[String: Any], Error>.Continuation,
                                             seen: Seen? = nil) async throws {
        guard let url = URL(string: base.trimmingCharacters(in: CharacterSet(charactersIn: "/")) + "/v1/" + op)
        else { throw PCError.unreachable("bad address") }
        let stamp = String(Int(Date().timeIntervalSince1970))
        let aad = "fixer1|\(op)|\(link.id)|\(stamp)"
        guard let sealed = try AES.GCM.seal(body, using: key, authenticating: Data(aad.utf8)).combined else { throw PCError.bad }
        var req = URLRequest(url: url, timeoutInterval: 90)
        req.httpMethod = "POST"
        req.httpBody = sealed
        req.setValue(link.id, forHTTPHeaderField: "X-Fixer-Phone")
        req.setValue(stamp, forHTTPHeaderField: "X-Fixer-Time")
        req.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        let (bytes, response) = try await session.bytes(for: req)
        guard let http = response as? HTTPURLResponse else { throw PCError.bad }
        if http.statusCode == 401 { throw PCError.refused }
        if meansUnreachable(http.statusCode) { throw URLError(.cannotConnectToHost) }
        guard http.statusCode == 200 else { throw PCError.server("HTTP \(http.statusCode)") }

        var it = bytes.makeAsyncIterator()
        var seq = 0
        var stopped: String?, paused = false, gone = false
        while true {
            guard let head = try await read(4, from: &it) else { throw PCError.dropped }
            let n = head.reduce(0) { $0 << 8 | Int($1) }
            guard n > 28, n < 200_000_000, let frame = try await read(n, from: &it) else { throw PCError.bad }
            let box = try AES.GCM.SealedBox(combined: frame)
            let plain = try AES.GCM.open(box, using: key, authenticating: Data("\(aad)|\(seq)".utf8))
            seq += 1
            guard let obj = try JSONSerialization.jsonObject(with: plain) as? [String: Any],
                  let t = obj["t"] as? String else { throw PCError.bad }
            if t == "done" { return }
            if t == "info" {
                if let why = obj["stopped"] as? String { stopped = why }
                if obj["brain_paused"] != nil { paused = true }
                if obj["gone"] != nil { gone = true }
            }
            if t == "error" {
                let v = obj["v"] as? String ?? "failed"
                throw stopped != nil ? PCError.stopped(v) : paused ? PCError.paused(v) : gone ? PCError.gone(v) : PCError.server(v)
            }
            seen?.got = true
            out.yield(obj)
        }
    }

    private nonisolated static func read(_ n: Int, from it: inout URLSession.AsyncBytes.Iterator) async throws -> Data? {
        var d = Data(capacity: n)
        while d.count < n {
            guard let b = try await it.next() else { return nil }
            d.append(b)
        }
        return d
    }

    // MARK: jobs

    /// A chat turn on the PC's brain. The PC keeps no memory, so the conversation so far travels with it.
    func chat(history: [(role: String, text: String)], text: String, image: CGImage?,
              slot: String? = nil,
              status: @escaping (String) -> Void,
              thought: @escaping (String) -> Void = { _ in }) throws -> AsyncThrowingStream<String, Error> {
        guard let link else { throw PCError.notPaired }
        let rules = Rules.shared
        var messages: [[String: Any]] = history.map { ["role": $0.role, "text": $0.text] }
        var turn: [String: Any] = ["role": "user", "text": text]
        if let image, let jpeg = Self.jpeg(image, longest: 1280) { turn["image"] = jpeg }
        messages.append(turn)
        var payload: [String: Any] = ["messages": messages, "temperature": rules.temperature, "max_tokens": rules.maxTokens,
                                      "model": rules.pcBrainID, "think": rules.thinkMode]
        if let i = Brain.fullInstructions() { payload["instructions"] = i }
        payload["device"] = Self.deviceFacts()       // 11-17: what the computer says when asked about this phone
        payload["caps"] = ["queue"]            // 11-17: this phone can run the line actions the brain may ask for
        if let slot { payload["slot"] = slot }
        let frames = call("chat", payload, link: link)
        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    for try await f in frames {
                        if f["t"] as? String == "text", let v = f["v"] as? String { continuation.yield(v) }
                        if f["t"] as? String == "status", let v = f["v"] as? String { status(v) }
                        if f["t"] as? String == "thought", let v = f["v"] as? String { thought(v) }
                    }
                    continuation.finish()
                } catch { continuation.finish(throwing: error) }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    /// A few plain facts about this phone, sent with each chat so "what phone am I on" can be answered exactly.
    /// Read fresh every time, kept nowhere; the computer uses it only for that question.
    @MainActor static func deviceFacts() -> [String: Any] {
        let dev = UIDevice.current
        dev.isBatteryMonitoringEnabled = true
        var model = [UInt8](repeating: 0, count: 64)
        var size = model.count
        sysctlbyname("hw.machine", &model, &size, nil, 0)
        let identifier = String(decoding: model.prefix { $0 != 0 }, as: UTF8.self)
        var out: [String: Any] = ["model": identifier, "ios": dev.systemVersion,
                                  "low_power": ProcessInfo.processInfo.isLowPowerModeEnabled ? "on" : "off"]
        if let v = try? URL(fileURLWithPath: NSHomeDirectory()).resourceValues(
            forKeys: [.volumeAvailableCapacityForImportantUsageKey, .volumeTotalCapacityKey]) {
            if let free = v.volumeAvailableCapacityForImportantUsage { out["storage_free_gb"] = (Double(free) / 1e9 * 10).rounded() / 10 }
            if let total = v.volumeTotalCapacity { out["storage_total_gb"] = (Double(total) / 1e9 * 10).rounded() / 10 }
        }
        if dev.batteryLevel >= 0 { out["battery"] = Double(dev.batteryLevel) }
        switch dev.batteryState {
        case .charging: out["battery_state"] = "charging"
        case .full: out["battery_state"] = "full"
        case .unplugged: out["battery_state"] = "on battery"
        default: break
        }
        switch ProcessInfo.processInfo.thermalState {
        case .nominal: out["thermal"] = "normal"
        case .fair: out["thermal"] = "slightly warm"
        case .serious: out["thermal"] = "hot"
        case .critical: out["thermal"] = "very hot"
        @unknown default: break
        }
        let info = Bundle.main.infoDictionary
        if let v = info?["CFBundleShortVersionString"] as? String { out["app_version"] = v }
        if let b = info?["CFBundleVersion"] as? String { out["app_build"] = b }
        return out
    }

    /// What the computer said about the last picture it made for the chat (the seed, and which recipe Auto picked).
    var lastImageSeed: Int?
    var lastImageRecipe: String?
    /// 11-17: the prompt the computer's chat model wrote from the owner's words ("Enhance my prompt"), when it did.
    var lastImageEnhanced: String?

    /// One image from the PC: a new one from `prompt`, or an edit of `start`. `explicit` is the chat model's call on
    /// whether the request is nude or sexual (nil = it didn't say); `enhance` asks the computer to enrich the prompt.
    func image(prompt: String, start: CGImage?, slot: String? = nil, explicit: Bool? = nil, enhance: Bool = false,
               queued: @escaping @Sendable (String) -> Void = { _ in },
               status: @escaping @Sendable (String) -> Void,
               progress: @escaping @Sendable (Double) -> Void,
               preview: @escaping @Sendable (Data) -> Void = { _ in }) async throws -> CGImage {
        guard let link else { throw PCError.notPaired }
        let rules = Rules.shared
        lastImageSeed = nil
        lastImageRecipe = nil
        lastImageEnhanced = nil
        let choice = rules.chatModelChoice
        // "model" stays for older computers: the chosen recipe's own model, otherwise the saved picture model.
        let legacyModel = choice == "auto" ? rules.pcImageID
            : (recipeCache?.first { $0.id == choice }?.model ?? rules.pcImageID)
        var payload: [String: Any] = ["prompt": prompt, "steps": rules.pcSteps, "guidance": rules.pcGuidance,
                                      "strength": rules.editStrength, "model": legacyModel, "model_choice": choice,
                                      "longest": rules.pcSize, "width": rules.pcSize, "height": rules.pcSize]
        if let explicit { payload["explicit"] = explicit }
        // 11-19: Explicit off this session -> the computer uses no explicit model, editor or add-on, whatever the words.
        payload["safe"] = !rules.explicitNow
        if enhance { payload["enhance"] = true }
        if let start {
            guard let png = Self.png(start) else { throw PCError.bad }
            payload["image"] = png
        }
        for try await f in jobStream("image", payload, slot: slot, link: link, queued: queued) {
            switch f["t"] as? String {
            case "progress": progress(f["v"] as? Double ?? 0)
            case "status": status(f["v"] as? String ?? "")
            case "enhanced": lastImageEnhanced = f["v"] as? String
            case "preview":
                if let b = f["v"] as? String, let d = Data(base64Encoded: b) { preview(d) }
            case "image":
                guard let b64 = f["v"] as? String, let data = Data(base64Encoded: b64),
                      let cg = UIImage(data: data)?.cgImage else { throw PCError.bad }
                lastImageSeed = (f["seed"] as? NSNumber)?.intValue
                lastImageRecipe = f["recipe"] as? String
                return cg
            default: break
            }
        }
        throw PCError.server("no image came back")
    }

    /// A short video from the computer: from `prompt`, or animating `start` when given. Returns MP4 data.
    /// `explicit` is "off", "on" or "auto" (the chat always sends "auto": the computer decides from the words).
    func video(prompt: String, start: CGImage?, explicit: String, slot: String? = nil,
               style: String? = nil, loras: [LoraSetting] = [], seconds: Int = 5,
               queued: @escaping @Sendable (String) -> Void = { _ in },
               status: @escaping @Sendable (String) -> Void,
               progress: @escaping @Sendable (Double) -> Void) async throws -> Data {
        guard let link else { throw PCError.notPaired }
        var payload: [String: Any] = ["prompt": prompt, "model": Rules.shared.pcVideoID,
                                      "explicit": explicit, "seconds": seconds,
                                      "safe": !Rules.shared.explicitNow]       // 11-19: see image()
        if let style { payload["style"] = style }
        if !loras.isEmpty {
            payload["loras"] = loras.map { l -> [String: Any] in
                var d: [String: Any] = ["name": l.name, "strength": l.strength]
                if let t = l.triggers { d["triggers"] = t }
                return d
            }
        }
        if let start {
            guard let jpeg = Self.jpeg(start, longest: 1280) else { throw PCError.bad }
            payload["image"] = jpeg
        }
        for try await f in jobStream("video", payload, slot: slot, link: link, queued: queued) {
            switch f["t"] as? String {
            case "progress": progress(f["v"] as? Double ?? 0)
            case "status": status(f["v"] as? String ?? "")
            case "video":
                guard let b64 = f["v"] as? String, let data = Data(base64Encoded: b64) else { throw PCError.bad }
                return data
            default: break
            }
        }
        throw PCError.server("No video came back. Try again.")
    }

    /// What to keep for a cut-out, as a mask the size of `photo`. `boxes` (0–1) limit it to those things; none =
    /// the main subject. The computer picks a hair-and-fur model for people and animals, a crisp one otherwise.
    func cutoutMask(_ photo: CGImage, boxes: [CGRect], labels: [String], slot: String? = nil,
                    queued: @escaping @Sendable (String) -> Void = { _ in },
                    status: @escaping @Sendable (String) -> Void) async throws -> CGImage {
        guard let link else { throw PCError.notPaired }
        guard let jpeg = Self.jpeg(photo, longest: 2560) else { throw PCError.bad }
        let payload: [String: Any] = ["image": jpeg, "labels": labels,
                                      "boxes": boxes.map { [Double($0.minX), Double($0.minY), Double($0.maxX), Double($0.maxY)] }]
        for try await f in jobStream("cutout", payload, slot: slot, link: link, queued: queued) {
            switch f["t"] as? String {
            case "status": status(f["v"] as? String ?? "")
            case "mask":
                guard let b64 = f["v"] as? String, let data = Data(base64Encoded: b64),
                      let m = UIImage(data: data)?.cgImage,
                      let full = ImageKit.resized(m, to: CGSize(width: photo.width, height: photo.height))
                else { throw PCError.bad }
                return full
            default: break
            }
        }
        throw PCError.server("No cut-out came back. Try again.")
    }

    /// A 4x larger, sharper photo from the computer's photo upscaler (longest side up to 8192 px).
    func upscale(_ photo: CGImage, slot: String? = nil,
                 queued: @escaping @Sendable (String) -> Void = { _ in },
                 status: @escaping @Sendable (String) -> Void,
                 progress: @escaping @Sendable (Double) -> Void) async throws -> CGImage {
        guard let link else { throw PCError.notPaired }
        guard let jpeg = Self.jpeg(photo, longest: CGFloat(max(photo.width, photo.height)), quality: 0.95)
        else { throw PCError.bad }
        for try await f in jobStream("upscale", ["image": jpeg], slot: slot, link: link, queued: queued) {
            switch f["t"] as? String {
            case "progress": progress(f["v"] as? Double ?? 0)
            case "status": status(f["v"] as? String ?? "")
            case "image":
                guard let b64 = f["v"] as? String, let data = Data(base64Encoded: b64),
                      let cg = UIImage(data: data)?.cgImage else { throw PCError.bad }
                return cg
            default: break
            }
        }
        throw PCError.server("No upscaled photo came back. Try again.")
    }

    /// Web search through the PC (works wherever the PC is reachable).
    func search(_ query: String, count: Int) async throws -> [[String: Any]] {
        guard let link else { throw PCError.notPaired }
        for try await f in call("search", ["query": query, "count": count], link: link) where f["t"] as? String == "info" {
            return f["results"] as? [[String: Any]] ?? []
        }
        return []
    }

    /// What the PC said after "forget": what it erased, and anything it found still there afterwards.
    struct ForgetReport: Sendable { var erased: [String]; var left: [String] }

    /// Tells the PC to erase everything it holds about this phone's conversations (chat model memory, the picture
    /// and video engine with any photo it was working on, their temporary files). The report once the PC confirms;
    /// nil after `seconds` (PC off or away: it stays owed and goes first next time).
    func forget(seconds: Double = 20) async -> ForgetReport? {
        guard let link else { return nil }
        let work = Task { () -> ForgetReport? in
            do {
                for try await f in self.call("forget", [:], link: link) where f["forgot"] as? Bool == true {
                    return ForgetReport(erased: f["erased"] as? [String] ?? ["Its memory of your conversations"],
                                        left: f["left"] as? [String] ?? [])
                }
            } catch PCError.refused {
                self.forgetOwed = false        // the PC doesn't know this phone: nothing of it there to erase
            } catch {}                         // anything else: stays owed until the PC confirms
            return nil
        }
        let timer = Task {
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            work.cancel()
        }
        let report = (try? await work.value) ?? nil
        timer.cancel()
        if report != nil { forgetOwed = false }
        return report
    }

    /// The models downloaded on the PC, by Hugging Face id, with their size in bytes.
    func installedModels() async throws -> [String: Double] {
        guard let link else { throw PCError.notPaired }
        for try await f in call("status", [:], link: link) where f["t"] as? String == "info" {
            let sizes = f["model_sizes"] as? [String: Any] ?? [:]
            return sizes.compactMapValues { ($0 as? NSNumber)?.doubleValue }
        }
        throw PCError.bad
    }

    /// Deletes a downloaded model from the PC. Returns a one-line report (space freed, folder now).
    func removeModel(_ id: String) async throws -> String {
        guard let link else { throw PCError.notPaired }
        for try await f in call("remove_model", ["model": id], link: link) where f["t"] as? String == "info" {
            let freed = f["freed_gb"] as? Double ?? 0, now = f["models_gb"] as? Double ?? 0, cap = f["cap_gb"] as? Double ?? 0
            return "Removed from your computer: \(Int(freed.rounded())) GB freed (\(Int(now.rounded())) of \(Int(cap)) GB used)."
        }
        throw PCError.bad
    }

    /// Every model on the computer the Models screen can remove: one entry per model (picture, video, chat, other).
    func installedInventory() async throws -> [InstalledModel] {
        guard let link else { throw PCError.notPaired }
        for try await f in call("installed", [:], link: link) where f["t"] as? String == "info" {
            return (f["installed"] as? [[String: Any]] ?? []).compactMap(InstalledModel.init)
        }
        throw PCError.bad
    }

    // MARK: Create screen (ComfyUI recipes on the computer)

    /// The recipes the computer can run, with the controls to show for each, and its LoRA library (`library`; an
    /// older computer sends file names only, which become entries of unknown family).
    func createRecipes() async throws -> (recipes: [CreateRecipe], loras: [String], library: [LoraEntry], videoFamily: String) {
        guard let link else { throw PCError.notPaired }
        for try await f in call("comfy_recipes", [:], link: link) where f["t"] as? String == "info" {
            let recipes = (f["recipes"] as? [[String: Any]] ?? []).compactMap(CreateRecipe.init)
            recipeCache = recipes
            let names = f["loras"] as? [String] ?? []
            var library = LoraEntry.rows(f["lora_library"])
            if library.isEmpty, f["lora_library"] == nil { library = names.map { LoraEntry(file: $0) } }
            return (recipes, names, library, f["video_lora_family"] as? String ?? "wan22")
        }
        throw PCError.bad
    }

    // MARK: LoRA library (add-ons kept on the computer)

    /// Every LoRA on the computer, and whether a Civitai key is saved there (the key itself never comes back).
    func loraList() async throws -> (loras: [LoraEntry], hasKey: Bool) {
        guard let link else { throw PCError.notPaired }
        for try await f in call("lora_list", [:], link: link) where f["t"] as? String == "info" {
            return (LoraEntry.rows(f["library"]), f["civitai_key"] as? Bool ?? false)
        }
        throw PCError.bad
    }

    /// One page of LoRAs for `family` (or the wider `families`), from Civitai or Hugging Face. `cursor` nil = the
    /// first page. `sort` is "popular"/"rated"/"new"; `period` ("week"/"month"/…) makes a sort "trending";
    /// `category` browses a Civitai tag (e.g. "character").
    func loraSearch(source: String, family: String, query: String, cursor: String?,
                    families: [String]? = nil, sort: String = "popular",
                    period: String? = nil, category: String? = nil)
        async throws -> (results: [LoraSearchResult], cursor: String?) {
        guard let link else { throw PCError.notPaired }
        var payload: [String: Any] = ["source": source, "family": family, "query": query, "sort": sort,
                                      "safe": !Rules.shared.explicitNow]    // 11-19: 18+ off -> no NSFW results
        if let cursor { payload["cursor"] = cursor }
        if let families, !families.isEmpty { payload["families"] = families }
        if let period { payload["period"] = period }
        if let category, !category.isEmpty { payload["category"] = category }
        for try await f in call("lora_search", payload, link: link) where f["t"] as? String == "info" {
            let results = (f["loras"] as? [[String: Any]] ?? []).compactMap { LoraSearchResult($0) }
            var next = f["cursor"] as? String
            if next == nil, let n = f["cursor"] as? NSNumber { next = n.stringValue }
            return (results, next)
        }
        throw PCError.bad
    }

    /// A search result's small preview, fetched through the computer (nothing is kept on the phone).
    func loraThumb(_ thumb: String) async throws -> UIImage? {
        guard let link else { throw PCError.notPaired }
        for try await f in call("lora_thumb", ["url": thumb], link: link) where f["t"] as? String == "info" {
            guard let b64 = f["thumb"] as? String, let data = Data(base64Encoded: b64) else { return nil }
            return UIImage(data: data)
        }
        return nil
    }

    /// What a pasted link (`link`) or a search result (`ref`) holds: a card to confirm, or several files to choose
    /// from first. Each choice carries the link that leads to just that file.
    func loraInspect(link text: String?, ref: String?) async throws -> LoraInspection {
        guard let link else { throw PCError.notPaired }
        var payload: [String: Any] = [:]
        if let text { payload["link"] = text }
        if let ref { payload["source_id"] = ref }
        for try await f in call("lora_inspect", payload, link: link) where f["t"] as? String == "info" {
            let card = (f["card"] as? [String: Any]).flatMap { LoraCard($0) }
            let names = f["choices"] as? [String] ?? []
            let links = f["links"] as? [String] ?? []
            let repo = f["repo"] as? String
            var choices: [LoraChoice] = []
            for (i, name) in names.enumerated() {
                let short = name.split(separator: "/").last.map(String.init) ?? name
                if i < links.count {
                    choices.append(LoraChoice(id: links[i], name: short))
                } else if let repo {
                    choices.append(LoraChoice(id: "https://huggingface.co/\(repo)/blob/main/\(name)", name: short))
                }
            }
            return LoraInspection(card: card, choices: choices)
        }
        throw PCError.bad
    }

    /// Downloads a LoRA to the computer and adds it to the library, from the same `link` or `ref` its card was
    /// read from. Cancelling the task drops the connection, and the computer then throws the half-downloaded file
    /// away.
    func loraAdd(link text: String?, ref: String?, family: String,
                 status: @escaping (String) -> Void,
                 progress: @escaping (Double) -> Void) async throws -> LoraEntry {
        guard let link else { throw PCError.notPaired }
        var payload: [String: Any] = [:]
        if let text { payload["link"] = text }
        if let ref { payload["source_id"] = ref }
        if !family.isEmpty { payload["family"] = family }
        for try await f in call("lora_add", payload, link: link) {
            switch f["t"] as? String {
            case "progress": progress((f["v"] as? NSNumber)?.doubleValue ?? 0)
            case "status": status(f["v"] as? String ?? "")
            case "info":
                let files = (f["added"] as? [[String: Any]] ?? []).compactMap { $0["file"] as? String }
                guard let first = files.first else { break }
                let rows = LoraEntry.rows(f["library"])
                return rows.first { files.contains($0.file) } ?? LoraEntry(file: first)
            default: break
            }
        }
        try Task.checkCancellation()
        throw PCError.server("The add-on didn't finish downloading. Try again.")
    }

    /// Changes a LoRA's usual strength, its "always use" switch or the model family it is filed under.
    func loraSet(file: String, strength: Double? = nil, alwaysOn: Bool? = nil,
                 family: String? = nil, explicit: Bool? = nil) async throws -> LoraEntry {
        guard let link else { throw PCError.notPaired }
        var payload: [String: Any] = ["file": file]
        if let strength { payload["strength"] = strength }
        if let alwaysOn { payload["always_on"] = alwaysOn }
        if let explicit { payload["explicit"] = explicit }
        if let family { payload["family"] = family }
        for try await f in call("lora_set", payload, link: link) where f["t"] as? String == "info" {
            let all = (f["library"] as? [[String: Any]] ?? []).compactMap { LoraEntry($0) }
            if let entry = all.first(where: { $0.file == file }) { return entry }
        }
        throw PCError.bad
    }

    /// Deletes a LoRA (both files of a video pair) from the computer.
    func loraRemove(_ file: String) async throws {
        guard let link else { throw PCError.notPaired }
        for try await f in call("lora_remove", ["file": file], link: link) {
            if f["t"] as? String == "info" { return }
        }
        throw PCError.bad
    }

    /// Saves the optional Civitai key on the computer ("" removes it). Returns whether one is saved now.
    func loraKey(_ key: String) async throws -> Bool {
        guard let link else { throw PCError.notPaired }
        for try await f in call("lora_key", ["key": key], link: link) where f["t"] as? String == "info" {
            return f["civitai_key"] as? Bool ?? !key.isEmpty
        }
        throw PCError.bad
    }

    /// The style recipes and quick picks for Make (the computer re-reads its file on every call, so
    /// edits there show up the next time Make opens). Held in memory only.
    func makeRecipes() async throws -> MakeCatalog {
        guard let link else { throw PCError.notPaired }
        for try await f in call("make_recipes", [:], link: link) where f["t"] as? String == "info" {
            return MakeCatalog(f)
        }
        throw PCError.bad
    }

    /// The recipes as last fetched this session (the chat's model cards use them; no second request per screen).
    private(set) var recipeCache: [CreateRecipe]?

    /// The picture models the chat can be told to use. Empty when the computer can't be reached.
    func imageRecipes() async -> [CreateRecipe] {
        if let recipeCache { return recipeCache.filter { $0.kind != "video" } }
        guard isPaired, let r = try? await createRecipes() else { return [] }
        return r.recipes.filter { $0.kind != "video" }
    }

    /// Runs a recipe: `onEvent` gets status lines, progress, live previews and each finished picture.
    func create(recipe: String, values: [String: Any], slot: String? = nil,
                queued: @escaping @Sendable (String) -> Void = { _ in },
                onEvent: @escaping (CreateEvent) -> Void) async throws {
        guard let link else { throw PCError.notPaired }
        var enhanced: String?                       // 11-17: sent once, before drawing; goes with every picture of this run
        let safe = !Rules.shared.explicitNow                // 11-19: see image()
        for try await f in jobStream("comfy_run", ["recipe": recipe, "values": values, "safe": safe], slot: slot,
                                     link: link, queued: queued) {
            switch f["t"] as? String {
            case "status": onEvent(.status(f["v"] as? String ?? ""))
            case "progress": onEvent(.progress(f["v"] as? Double ?? 0))
            case "enhanced": enhanced = f["v"] as? String
            case "preview":
                if let b = f["v"] as? String, let d = Data(base64Encoded: b), let ui = UIImage(data: d) { onEvent(.preview(ui)) }
            case "image":
                guard let b = f["v"] as? String, let d = Data(base64Encoded: b), let ui = UIImage(data: d) else { throw PCError.bad }
                onEvent(.image(ui, seed: (f["seed"] as? NSNumber)?.intValue ?? 0, enhanced: enhanced))
            default: break
            }
        }
    }

    private var lastWarmUp: Date?
    private var lastMakeWarmUp: Date?

    /// Opening Fixer wakes the computer: it starts the chat model now (seconds), so the first message doesn't wait,
    /// and the address race runs now too. At most once a minute. (Until 11-17 this warmed the picture model, which
    /// held up the first chat for up to two minutes.)
    func warmUp(force: Bool = false) {
        guard isOn, let link, force || leaveSent || (lastWarmUp.map { Date().timeIntervalSince($0) > 60 } ?? true)
        else { return }
        // 11-19: away longer than the "free after" time: the computer has given its chat model back by now.
        if let left = leftAt, freeAfterMinutes > 0, Date().timeIntervalSince(left) > Double(freeAfterMinutes * 60) - 30 {
            brainReadyAt = nil
        }
        leftAt = nil
        lastWarmUp = Date()
        leaveSent = false                   // 11-18: coming back always tells the computer, cancelling a pending free
        let frames = call("warm", ["for": "chat", "brain": Rules.shared.pcBrainID,
                                   "free_after_min": freeAfterMinutes], link: link)
        Task.detached(priority: .userInitiated) {
            // 11-19: the first frame says at once whether the chat model is up; the last one comes when it is.
            // Until "ready" the phone answers on itself, so its own model loads now (only if it is already here).
            do {
                for try await f in frames {
                    if let s = f["brain_state"] as? String {
                        await MainActor.run { Desktop.shared.noteBrain(ready: s == "ready") }
                    } else if f["warm"] as? Bool == true {        // a computer from before 11-19: up after the warm
                        await MainActor.run { Desktop.shared.noteBrain(ready: true) }
                    }
                }
            } catch {
                await MainActor.run { Desktop.shared.noteBrain(ready: false) }
            }
        }
        Task { await refreshConfig() }
    }

    private var leaveSent = false
    private var leftAt: Date?

    /// 11-19: when the computer last said its chat model is up (or last answered a chat). Chat goes to the computer
    /// only while this is fresh; otherwise this iPhone answers at once and the computer takes over when it's ready.
    private var brainReadyAt: Date?

    /// True when a chat sent now should go to the computer: its model was up within the last 15 minutes (it unloads
    /// after 20 idle). False -> answer on this iPhone (see `Chat.think`), unless the iPhone has no model of its own.
    var pcBrainReady: Bool { brainReadyAt.map { Date().timeIntervalSince($0) < 15 * 60 } ?? false }

    /// Records what the computer said about its chat model. Not ready: the iPhone's own model loads in the
    /// background (only one already downloaded and loaded before, never a download), so the first answer is quick.
    func noteBrain(ready: Bool) {
        brainReadyAt = ready ? Date() : nil
        // Only while Fixer is on screen: a load during the background window after leaving could get it killed.
        if !ready, Brain.shared.hasLocalModel, UIApplication.shared.applicationState == .active {
            Task { try? await Brain.shared.load() }
        }
    }

    /// 11-18: the owner changed "free after" in Settings: tells the computer the number and nothing else. ("for":
    /// "settings" loads no model and does not start a queue job; an older computer ignores the unknown value.)
    func sendFreeAfter() {
        guard isOn, let link else { return }
        let frames = call("warm", ["for": "settings", "free_after_min": freeAfterMinutes], link: link)
        Task.detached(priority: .userInitiated) { do { for try await _ in frames {} } catch {} }
    }

    /// 11-18: the owner's "free my computer after I leave" choice (Settings): minutes, 0 = never; 5 until chosen.
    private var freeAfterMinutes: Int {
        let d = UserDefaults.standard
        return d.object(forKey: "pc.freeAfterMinutes") == nil ? 5 : d.integer(forKey: "pc.freeAfterMinutes")
    }

    /// 11-18: the app went to the background: tells the computer so it can give its memory back early. Fire and
    /// forget; an older computer answers 404, which is swallowed. A background task keeps the send alive after iOS
    /// suspends the app. Coming back calls warmUp(), which cancels the pending free on the computer.
    func leave() {
        guard isOn, let link else { return }
        leaveSent = true
        leftAt = Date()
        let app = UIApplication.shared
        let box = BackgroundTaskBox()
        box.id = app.beginBackgroundTask(withName: "fixer.leave") { Task { @MainActor in box.end() } }
        let frames = call("leave", ["free_after_min": freeAfterMinutes], link: link)
        Task.detached(priority: .userInitiated) {
            do { for try await _ in frames {} } catch {}
            await MainActor.run { box.end() }
        }
    }

    /// Holds one background-task id so the expiry handler and the finished send can each end it, once.
    final class BackgroundTaskBox: @unchecked Sendable {
        var id = UIBackgroundTaskIdentifier.invalid
        @MainActor func end() {
            guard id != .invalid else { return }
            UIApplication.shared.endBackgroundTask(id)
            id = .invalid
        }
    }

    /// The Make tab opened (or the chat's picture model changed): load the picture model ahead of the first
    /// picture. At most every 10 minutes unless forced.
    /// Stop everything: every picture, video and edit running or waiting on the computer stops now (11-17: abandoned
    /// requests kept the computer busy). Fire and forget; the Stop buttons call it too.
    func stopAll() {
        guard isOn, let link else { return }
        let frames = call("stop_all", [:], link: link)
        Task.detached(priority: .userInitiated) { for try await _ in frames {} }
    }

    // MARK: the computer's line (11-17)

    /// Whether a failure only means the computer is an older Fixer Desktop that doesn't know the line yet.
    nonisolated static func isOldComputer(_ e: Error) -> Bool {
        guard let p = e as? PCError, case .server(let s) = p else { return false }
        return s == "HTTP 404"
    }

    /// Was this failure a dropped connection (phone in the background, network change) rather than an answer?
    nonisolated static func isDrop(_ e: Error) -> Bool {
        if e is CancellationError { return false }
        if let p = e as? PCError {
            switch p {
            case .dropped, .unreachable, .bad: return true
            default: return false
            }
        }
        if let u = e as? URLError { return u.code != .cancelled }
        return true                     // CryptoKit / decoding errors from a half-written frame
    }

    /// Job ids a live task on this phone is still waiting for (the inbox never adopts those).
    nonisolated static let live = LiveJobs()

    /// A picture/edit/video request that waits in the computer's line. Gets a job id first; if the connection drops
    /// (the phone was in the background, the network changed) it re-attaches to the same job instead of failing, and
    /// skips results it already has. Single-result ops acknowledge their result so the computer drops it at once.
    nonisolated func jobStream(_ op: String, _ payload: [String: Any], slot: String?, link: PCLink,
                               queued: @escaping @Sendable (String) -> Void = { _ in }) -> AsyncThrowingStream<[String: Any], Error> {
        var p = payload
        if let slot { p["slot"] = slot }
        p["ack"] = true
        let first = call(op, p, link: link)
        let single = op != "comfy_run"
        return AsyncThrowingStream { continuation in
            let task = Task {
                var jobID: String?
                var seen = Set<Int>()
                var frames = first
                var tries = 0
                defer { if let id = jobID { Task { await Self.live.remove(id) } } }
                while true {
                    do {
                        for try await f in frames {
                            let t = f["t"] as? String
                            if t == "queued" {
                                if jobID == nil, let id = f["id"] as? String {
                                    jobID = id
                                    await Self.live.add(id)
                                    queued(id)
                                }
                                continue
                            }
                            if let n = (f["n"] as? NSNumber)?.intValue {
                                if seen.contains(n) { continue }
                                seen.insert(n)
                            }
                            continuation.yield(f)
                            if single, t == "image" || t == "video" || t == "mask", let id = jobID {
                                await Self.live.markCollected(id)
                                await self.ackNow(id, link: link)       // finished before the id leaves `live`
                            }
                        }
                        if !single, let id = jobID {
                            await Self.live.markCollected(id)
                            await self.ackNow(id, link: link)
                        }
                        continuation.finish()
                        return
                    } catch let e where Self.isDrop(e) && jobID != nil && !Task.isCancelled && tries < 40 {
                        tries += 1
                        try? await Task.sleep(nanoseconds: UInt64(min(10, 1 + tries)) * 1_000_000_000)
                        frames = self.call("queue", ["action": "watch", "id": jobID ?? ""], link: link)
                    } catch {
                        // 11-17: once a job id is known the computer still has the work; a plain "unreachable" would make
                        // the caller draw the same thing on this iPhone as well, so report it as a dropped connection.
                        if jobID != nil, !Task.isCancelled, let pe = error as? PCError, pe.isUnreachable {
                            continuation.finish(throwing: PCError.dropped)
                        } else {
                            continuation.finish(throwing: error)
                        }
                        return
                    }
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    /// Same, but waits for the computer to answer and tries once more if the first try fails (11-17).
    nonisolated func ackNow(_ id: String, link: PCLink) async {
        for attempt in 0..<2 {
            do {
                for try await _ in call("queue", ["action": "ack", "id": id], link: link) {}
                return
            } catch {
                if attempt == 0 { try? await Task.sleep(nanoseconds: 1_000_000_000) }
            }
        }
    }

    /// Tells the computer a result arrived, so it drops its held copy at once (fire and forget).
    nonisolated func ack(_ id: String, link: PCLink) {
        let frames = call("queue", ["action": "ack", "id": id], link: link)
        Task.detached(priority: .utility) { for try await _ in frames {} }
    }

    /// Re-attach to a job: its results so far, then live frames (used by the inbox for finished work).
    nonisolated func watch(_ id: String, link: PCLink) -> AsyncThrowingStream<[String: Any], Error> {
        call("queue", ["action": "watch", "id": id], link: link)
    }

    /// What the computer is making, what waits, what is held, and how full its memory is.
    func queueList() async throws -> PCQueue {
        guard let link else { throw PCError.notPaired }
        for try await f in call("queue", ["action": "list"], link: link) where f["t"] as? String == "info" {
            if let q = f["queue"] as? [String: Any] { return PCQueue(q) }
        }
        throw PCError.bad
    }

    /// Stops these jobs (running or waiting) or drops finished ones. How many it found.
    func cancelJobs(_ ids: [String]) async throws -> Int {
        guard let link else { throw PCError.notPaired }
        for try await f in call("queue", ["action": "cancel", "ids": ids], link: link) where f["t"] as? String == "info" {
            return (f["cancelled"] as? NSNumber)?.intValue ?? 0
        }
        return 0
    }

    /// Stops what one place (a chat, a Make card) has unfinished on the computer. Fire and forget; an older computer
    /// without the line gets "stop everything" instead.
    func cancel(slot: String) {
        guard isOn, let link else { return }
        let frames = call("queue", ["action": "cancel", "slot": slot], link: link)
        Task.detached(priority: .userInitiated) {
            do { for try await _ in frames {} }
            catch { if Desktop.isOldComputer(error) { await MainActor.run { Desktop.shared.stopAll() } } }
        }
    }

    /// Moves a waiting job to that place in line (1 = next). The place it ended up in.
    func moveJob(_ id: String, to: Int) async throws -> Int {
        guard let link else { throw PCError.notPaired }
        for try await f in call("queue", ["action": "move", "id": id, "to": to], link: link) where f["t"] as? String == "info" {
            return (f["position"] as? NSNumber)?.intValue ?? to
        }
        return to
    }

    /// Stop everything, and wait for the computer to say how many things it stopped.
    func stopAllNow() async throws -> Int {
        guard let link else { throw PCError.notPaired }
        for try await f in call("stop_all", [:], link: link) where f["t"] as? String == "info" {
            return (f["stopped"] as? NSNumber)?.intValue ?? 0
        }
        return 0
    }

    /// Stops everything and unloads every model from the computer's graphics card and memory. Nil on an older
    /// computer that doesn't know how.
    func freeMemory() async throws -> PCQueue? {
        guard let link else { throw PCError.notPaired }
        do {
            var out = PCQueue()
            for try await f in call("free_memory", [:], link: link) where f["t"] as? String == "info" {
                out = PCQueue(f)
            }
            return out
        } catch {
            if Desktop.isOldComputer(error) { return nil }
            throw error
        }
    }

    func warmUpMake(force: Bool = false) {
        guard isOn, let link, force || (lastMakeWarmUp.map { Date().timeIntervalSince($0) > 600 } ?? true) else { return }
        lastMakeWarmUp = Date()
        let frames = call("warm", ["for": "make", "model": Rules.shared.pcImageID,
                                   "model_choice": Rules.shared.chatModelChoice,
                                   "safe": !Rules.shared.explicitNow], link: link)   // 11-19: never preload 18+ when off
        Task.detached(priority: .background) { for try await _ in frames {} }
    }

    /// Texts, word lists and defaults the computer can change without an app update; the last copy is kept.
    func refreshConfig() async {
        guard let link else { return }
        do {
            for try await f in call("config", [:], link: link) where f["t"] as? String == "info" {
                if let c = f["config"] as? [String: Any] {
                    RemoteConfig.save(c)
                    Rules.shared.refreshDefaultPersonality()
                }
            }
        } catch {}
    }

    /// Asks the PC what it has loaded and how full the model folder is.
    func status() async throws -> String {
        guard let link else { throw PCError.notPaired }
        for try await f in call("status", [:], link: link) where f["t"] as? String == "info" {
            let gpu = f["gpu"] as? String ?? "graphics card"
            let used = f["models_gb"] as? Double ?? 0, cap = f["cap_gb"] as? Double ?? 0
            return "Connected · \(gpu) · \(Int(used)) of \(Int(cap)) GB used by models"
        }
        throw PCError.bad
    }

    nonisolated static func jpeg(_ cg: CGImage, longest: CGFloat, quality: CGFloat = 0.88) -> String? {
        let s = min(1, longest / CGFloat(max(cg.width, cg.height)))
        let img = s < 1 ? ImageKit.resized(cg, to: CGSize(width: CGFloat(cg.width) * s, height: CGFloat(cg.height) * s)) ?? cg : cg
        return UIImage(cgImage: img).jpegData(compressionQuality: quality)?.base64EncodedString()
    }

    nonisolated static func png(_ cg: CGImage) -> String? { UIImage(cgImage: cg).pngData()?.base64EncodedString() }
}

/// Web search via SearXNG: through the PC when it's on, otherwise straight to the NAS.
@MainActor
enum WebSearch {
    static func run(_ query: String) async throws -> String {
        let count = Rules.shared.searchResults
        let results: [[String: Any]]
        if Desktop.shared.isOn {
            results = try await Desktop.shared.search(query, count: count)
        } else {
            results = try await direct(query, count: count)
        }
        if results.isEmpty { return "(no results)" }
        return results.enumerated().map { i, r in
            let title = r["title"] as? String ?? ""
            let url = r["url"] as? String ?? ""
            let text = (r["content"] as? String ?? "").prefix(500)
            return "\(i + 1). \(title) — \(url)\n\(text)"
        }.joined(separator: "\n\n")
    }

    private static func direct(_ query: String, count: Int) async throws -> [[String: Any]] {
        let typed = Rules.shared.searchURL.trimmingCharacters(in: .whitespacesAndNewlines)
        var c = URLComponents(string: typed.isEmpty ? (RemoteConfig.string("search_url") ?? "") : typed)
        c?.path = "/search"
        c?.queryItems = [URLQueryItem(name: "q", value: query), URLQueryItem(name: "format", value: "json")]
        guard let url = c?.url else { throw PCError.unreachable("bad web search address (Settings → Web search)") }
        let config = URLSessionConfiguration.ephemeral
        config.urlCache = nil
        do {
            let (data, _) = try await URLSession(configuration: config).data(from: url)
            let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            return Array((obj?["results"] as? [[String: Any]] ?? []).prefix(count))
        } catch {
            throw PCError.unreachable("web search: \(error.localizedDescription)")
        }
    }
}

/// The address that answered last (home or away), for 30 s.
actor RouteMemory {
    private var best: String?
    private var when = Date.distantPast
    func current() -> String? { Date().timeIntervalSince(when) < 30 ? best : nil }
    func set(_ base: String) { best = base; when = Date() }
    func clear() { best = nil }
}

/// Job ids that a live task on this phone is still following.
actor LiveJobs {
    private var ids: Set<String> = []
    /// 11-17: ids this phone already took the result of, kept ~35 min (the computer holds a result at most 30 min).
    /// The computer keeps a job as "ready" until the acknowledgement lands, so without this the inbox could file a
    /// result a second time in that gap, or after an acknowledgement that never went out.
    private var collected: [String: Date] = [:]
    func add(_ id: String) { ids.insert(id) }
    func remove(_ id: String) { ids.remove(id) }
    func markCollected(_ id: String) {
        let now = Date()
        collected = collected.filter { now.timeIntervalSince($0.value) < 35 * 60 }
        collected[id] = now
    }
    func contains(_ id: String) -> Bool {
        if ids.contains(id) { return true }
        if let d = collected[id], Date().timeIntervalSince(d) < 35 * 60 { return true }
        return false
    }
}

/// One entry of the computer's line, as the computer reports it (11-17).
struct PCJob: Identifiable, Equatable {
    let id: String
    let kind: String
    let label: String
    let state: String            // running | waiting | ready | failed
    let position: Int?
    let progress: Double?
    let status: String
    let slot: String?
    let mine: Bool
    let etaSeconds: Int?
    let keepSeconds: Int?
    let error: String?

    init?(_ d: [String: Any]) {
        guard let id = d["id"] as? String, let state = d["state"] as? String else { return nil }
        self.id = id
        self.state = state
        kind = d["kind"] as? String ?? ""
        label = d["label"] as? String ?? ""
        position = (d["position"] as? NSNumber)?.intValue
        progress = (d["progress"] as? NSNumber)?.doubleValue
        status = d["status"] as? String ?? ""
        slot = d["slot"] as? String
        mine = d["mine"] as? Bool ?? false
        etaSeconds = (d["eta_s"] as? NSNumber)?.intValue
        keepSeconds = (d["keep_s"] as? NSNumber)?.intValue
        error = d["error"] as? String
    }
}

/// One finished job's real timings, as the computer measured them while it ran (11-17). No prompt text.
struct PCTiming: Equatable, Identifiable {
    let kind: String             // picture | pictures | edit | change | video | cutout | upscale
    let model: String
    let editor: String?
    let explicit: Bool
    let enhance: Bool
    let mp: Double?
    let steps: Int?
    let totalS: Double
    let drawS: Double
    let loadS: Double
    let enhanceS: Double
    let loaded: Bool             // the computer had to load a model for this job
    let ok: Bool
    let ageS: Int
    /// Derived from the fields: a random id would make every refresh look like a change.
    var id: String { "\(kind)|\(model)|\(ageS)|\(totalS)" }

    init?(_ d: [String: Any]) {
        guard let total = (d["total_s"] as? NSNumber)?.doubleValue else { return nil }
        kind = d["kind"] as? String ?? ""
        model = d["model"] as? String ?? ""
        editor = d["editor"] as? String
        explicit = d["explicit"] as? Bool ?? false
        enhance = d["enhance"] as? Bool ?? false
        mp = (d["mp"] as? NSNumber)?.doubleValue
        steps = (d["steps"] as? NSNumber)?.intValue
        totalS = total
        drawS = (d["draw_s"] as? NSNumber)?.doubleValue ?? 0
        loadS = (d["load_s"] as? NSNumber)?.doubleValue ?? 0
        enhanceS = (d["enhance_s"] as? NSNumber)?.doubleValue ?? 0
        loaded = d["loaded"] as? Bool ?? false
        ok = d["ok"] as? Bool ?? true
        ageS = (d["age_s"] as? NSNumber)?.intValue ?? 0
    }
}

/// What a kind of job usually takes on this computer since it started, per model.
struct PCTimingAverage: Equatable, Identifiable {
    let model: String
    let kind: String             // "new picture", "edit", "video"...
    let explicit: Bool
    let n: Int
    let totalS: Double
    let drawS: Double
    let warmTotalS: Double?      // when the model was already loaded
    let coldTotalS: Double?      // when it had to load
    var id: String { "\(model)|\(kind)|\(explicit)" }

    init?(_ d: [String: Any]) {
        guard let model = d["model"] as? String, let total = (d["total_s"] as? NSNumber)?.doubleValue else { return nil }
        self.model = model
        kind = d["kind"] as? String ?? ""
        explicit = d["explicit"] as? Bool ?? false
        n = (d["n"] as? NSNumber)?.intValue ?? 1
        totalS = total
        drawS = (d["draw_s"] as? NSNumber)?.doubleValue ?? 0
        warmTotalS = (d["warm_total_s"] as? NSNumber)?.doubleValue
        coldTotalS = (d["cold_total_s"] as? NSNumber)?.doubleValue
    }
}

/// The line plus how full the computer is.
struct PCQueue: Equatable {
    var jobs: [PCJob] = []
    var recent: [PCTiming] = []              // newest first, from an updated computer only
    var averages: [PCTimingAverage] = []
    var cardName = "", cardUsed = 0.0, cardTotal = 0.0, memUsed = 0.0, memTotal = 0.0
    var brainState = "off", brainWhy: String? = nil, brainModel = "", brainCard = 0.0, keepMinutes = 30

    init() {}

    init(_ d: [String: Any]) {
        jobs = (d["jobs"] as? [[String: Any]] ?? []).compactMap(PCJob.init)
        recent = (d["recent"] as? [[String: Any]] ?? []).compactMap(PCTiming.init)
        averages = (d["averages"] as? [[String: Any]] ?? []).compactMap(PCTimingAverage.init)
        if let c = d["card"] as? [String: Any] {
            cardName = c["name"] as? String ?? ""
            cardUsed = (c["used_gb"] as? NSNumber)?.doubleValue ?? 0
            cardTotal = (c["total_gb"] as? NSNumber)?.doubleValue ?? 0
        }
        if let m = d["memory"] as? [String: Any] {
            memUsed = (m["used_gb"] as? NSNumber)?.doubleValue ?? 0
            memTotal = (m["total_gb"] as? NSNumber)?.doubleValue ?? 0
        }
        if let b = d["brain"] as? [String: Any] {
            brainState = b["state"] as? String ?? "off"
            brainWhy = b["why"] as? String
            brainModel = b["model"] as? String ?? ""
            brainCard = (b["card_gb"] as? NSNumber)?.doubleValue ?? 0
        }
        keepMinutes = (d["keep_minutes"] as? NSNumber)?.intValue ?? 30
    }
}
