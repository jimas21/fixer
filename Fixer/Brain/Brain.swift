import CoreImage
import Foundation
import HuggingFace
import MLX
import MLXHuggingFace
import MLXLMCommon
import MLXVLM
import Tokenizers

/// The on-device vision-language model. It reads the owner's rules, looks at the photo, reasons about the
/// request, and answers with a short reply plus a list of actions Fixer then carries out.
@MainActor
final class Brain: ObservableObject {
    static let shared = Brain()

    enum State: Equatable {
        case idle, downloading(Double), loading, ready, failed(String)
    }

    @Published private(set) var state: State = .idle
    private var container: ModelContainer?
    private var loadedID: String?
    private var chat: ChatSession?

    /// Always added to the owner's rules: only how to ask for a web search. Otherwise it's a normal assistant.
    nonisolated static var baseInstructions: String { RemoteConfig.string("base_instructions") ?? builtinBaseInstructions }
    nonisolated static let builtinBaseInstructions = """
    Answer from what you know. Search the web only when the answer depends on facts that change or are newer than
    you (news, prices, weather, sports results, recent releases, opening hours) or when I ask you to look something
    up. Never search for greetings, tests, small talk, opinions, advice, writing, maths, code, explanations or
    general knowledge. When unsure, just answer. To search, reply with only {"tool": "search", "query": "..."}
    and you'll get the results, then answer.
    """

    /// Added only to messages about a photo or picture (a photo just attached, an area painted, or words like
    /// edit / remove / draw). Shown in Settings.
    nonisolated static var photoTools: String { RemoteConfig.string("photo_tools") ?? builtinPhotoTools }
    nonisolated static let builtinPhotoTools = """
    Image and video tools are available for this message. If the user wants the current photo changed, a new
    picture made, or a video made, reply with ONE JSON object and nothing else:
    {"say": "your reply in your own voice: one to three sentences on what you're making and why",
     "actions": [ ... ]}
    Actions (use as many as the request needs, in order):
    {"tool": "remove",  "targets": [T, ...]}            erase people/objects and fill the background
    {"tool": "cutout",  "targets": [T, ...]}            keep only these, make the rest transparent ([] = main subject)
    {"tool": "redraw",  "prompt": "...", "targets": [T, ...], "explicit": false}  repaint these areas (or the whole photo if []) as described
    {"tool": "upscale"}                                 4x sharper, larger photo
    {"tool": "make",    "prompt": "...", "explicit": false}            new image from a description
    {"tool": "video",   "prompt": "...", "from_photo": false, "explicit": false}  short video clip (from_photo true animates the current photo)
    Add "line_up": true to a make, redraw or video action only when I ask for it in addition to what this chat is already making; otherwise a new one replaces this chat's unfinished one.
    Set "explicit" to true on a make, redraw or video when what I asked to see is nude or sexual, false otherwise. It switches the explicit add-on on my computer; it never changes the words.
    T = {"label": "what it is", "box": [x1, y1, x2, y2]} with coordinates from 0 to 1000 relative to the photo
    (0,0 top-left, 1000,1000 bottom-right). Box every matching thing separately.
    If the user has painted an area, targets may be [] and the painted area is used.
    Write redraw/make/video prompts by starting with the user's own words for the picture, unchanged, then adding
    visual detail (setting, light, camera, style; for video, the motion). Keep everything the user asked for, exactly
    as explicit as they asked: never add clothing, covering or anything they didn't ask for, and never tone a request
    down.
    If the user is only asking about the photo or chatting, answer in plain text.
    """

    private init() {}

    var isReady: Bool { state == .ready }

    /// 11-19: true once this model has loaded on this iPhone at least once, i.e. it is downloaded. Only then does
    /// chat answer here while the computer warms up, and only then is it preloaded (never a surprise download).
    var hasLocalModel: Bool { UserDefaults.standard.bool(forKey: "brain.local." + Rules.shared.brainID) }

    private var loading: Task<Void, Error>?
    private var loadingID: String?

    /// Loads the model if it isn't already. 11-19: callers arriving during a load (the preload when the computer
    /// isn't ready, then the first message) wait for that same load instead of starting a second one.
    func load() async throws {
        let id = Rules.shared.brainID
        if container != nil, loadedID == id { return }
        if let loading, loadingID == id { try await loading.value; return }
        let t = Task { try await self.loadNow(id) }
        loading = t
        loadingID = id
        defer { if loadingID == id { loading = nil; loadingID = nil } }
        try await t.value
    }

    private func loadNow(_ id: String) async throws {
        if container != nil, loadedID == id { return }
        container = nil
        chat = nil
        MLX.Memory.cacheLimit = 32 * 1024 * 1024
        state = .downloading(0)
        do {
            let config = ModelConfiguration(id: id, extraEOSTokens: ["<|im_end|>"])
            let c = try await VLMModelFactory.shared.loadContainer(
                from: #hubDownloader(), using: #huggingFaceTokenizerLoader(),
                configuration: config, useLatest: false
            ) { progress in
                let f = progress.fractionCompleted
                Task { @MainActor in
                    if case .downloading = Brain.shared.state {
                        Brain.shared.state = f >= 0.999 ? .loading : .downloading(f)
                    }
                }
            }
            container = c
            loadedID = id
            state = .ready
            UserDefaults.standard.set(true, forKey: "brain.local." + id)
        } catch {
            state = .failed(error.localizedDescription)
            throw error
        }
    }

    /// Frees the model's memory (used before the image generator runs).
    func unload() {
        container = nil
        chat = nil
        loadedID = nil
        if state == .ready { state = .idle }
    }

    private var parameters: GenerateParameters {
        GenerateParameters(maxTokens: Rules.shared.maxTokens, temperature: Float(Rules.shared.temperature),
                           topP: 0.8)   // Qwen's chat setting (the PC also uses top_k 20)
    }

    private func instructions(onPC: Bool = false) -> String {
        let own = Rules.shared.instructions.trimmingCharacters(in: .whitespacesAndNewlines)
        // The facts block goes only to the computer, which keeps it just for messages that ask about Fixer itself;
        // on the phone the model volunteered it in ordinary replies (the owner 11-16: "I hate this pre-baked meta").
        return [own.isEmpty ? nil : own, onPC ? Self.about(onPC: true) : nil, Self.baseInstructions]
            .compactMap { $0 }.joined(separator: "\n\n")
    }

    /// The owner's rules, the facts about where this model runs, and the search line, for the PC brain.
    static func fullInstructions() -> String? { shared.instructions(onPC: true) }

    /// Plain facts the model can use when asked where it runs, which model it is, or what it can do.
    /// Built fresh for every conversation so it always matches the current settings.
    static func about(onPC: Bool) -> String {
        let pc = Desktop.shared.isOn
        let here = onPC ? "" : " You are running on the owner's iPhone, entirely on the device."
        let video = pc ? "make short videos" : "make short videos when the owner's computer is connected"
        return """
        About you (only when asked who you are or what you can do; say it in a sentence or two, in your own voice):
        - Who you are: Fixer, the owner's personal assistant. You take care of whatever he needs. Confident, \
        laid-back, a bit of swagger, never corporate.
        - What you can do: answer and reason, plan, write, research, look at photos, search the web, make new \
        pictures, change or redraw parts of a photo, remove things, cut out a subject, sharpen and enlarge, \
        \(video), and run the work on his computer.
        - Never bring up where you run, links, privacy, clouds or hardware unless he asks about exactly that.\(here)
        """
    }

    // MARK: The conversation

    func resetChat() { chat = nil }

    /// "Clear everything": drops the conversation and empties MLX's buffer cache, where the freed conversation
    /// memory would otherwise sit until reused. The model itself stays loaded (it holds no conversation).
    func forget() {
        chat = nil
        sessionInstructions = nil
        MLX.Memory.clearCache()
    }

    /// The instructions the current phone session was built with. If they no longer match (settings changed,
    /// or the phone is taking over from the computer), the session is rebuilt from the recap.
    private var sessionInstructions: String?

    /// One turn of the ongoing conversation. `image` = the current photo, sent only when the brain hasn't seen
    /// this version yet; `photo` = the current photo regardless, re-sent if the session has to be rebuilt.
    /// `recap` is used only if the brain's memory had to be rebuilt (new session).
    func stream(_ text: String, image: CGImage?, photo: CGImage?, recap: String) async throws -> AsyncThrowingStream<String, Error> {
        try await load()
        guard let container else { throw ToolError.model("The chat model isn't ready yet.") }
        var prompt = text
        var image = image
        let current = instructions()
        if chat != nil, sessionInstructions != current { chat = nil }
        if chat == nil {
            sessionInstructions = current
            chat = ChatSession(container, instructions: current, generateParameters: parameters,
                               processing: .init(resize: CGSize(width: 768, height: 768)))
            if !recap.isEmpty { prompt = "Earlier in this conversation:\n\(recap)\n\nNow: \(text)" }
            if image == nil { image = photo }
        }
        chat?.generateParameters = parameters
        let images: [UserInput.Image] = image.map { [.ciImage(CIImage(cgImage: $0))] } ?? []
        if image != nil { prompt += "\n(This is the current photo.)" }
        return chat!.streamResponse(to: prompt, images: images)
    }
}

// MARK: - Plan

struct Plan {
    struct Target { var label: String; var box: CGRect? }   // box in 0...1 relative coordinates
    enum Action {
        case remove([Target]), cutout([Target]), redraw(String, [Target]), upscale, make(String), search(String)
        case video(String, fromPhoto: Bool)
        case queueCancel([String]), queueMove(String, to: Int), stopAll, freeMemory      // 11-17: the computer's line

        /// Managing the computer's line: quick, runs before any picture work, never waits.
        var isQueueControl: Bool {
            switch self {
            case .queueCancel, .queueMove, .stopAll, .freeMemory: return true
            default: return false
            }
        }
    }
    var say: String
    var actions: [Action]
    /// "line_up": true -> this chat's new job waits behind its unfinished one instead of replacing it (11-17).
    var lineUp = false
    /// "explicit": the brain's own call on whether the request is nude or sexual (the first action that says), which
    /// switches the explicit add-on on the computer. nil = it didn't say (old behaviour).
    var explicit: Bool? = nil

    /// Words that put clothes on a picture. If the chat model's prompt has one the user didn't use, it softened the
    /// request; the user's own words are used instead (research 11-16: abliterated models still "tidy up" prompts).
    /// Clothing-specific only: "covered" / "dressed" would also match "snow-covered peaks" or "a dressed salad".
    static var coveringWords: [String] { RemoteConfig.list("covering_words") ?? builtinCoveringWords }
    static let builtinCoveringWords = ["bikini", "swimsuit", "swimwear", "bathing suit", "lingerie", "underwear", "bra",
                                "panties", "briefs", "towel", "robe", "clothed", "fully clothed", "tasteful",
                                "modest", "censored", "implied nudity"]

    /// The picture prompts in this plan as the owner wants them: his own words when "Use my words" is on, or when
    /// the chat model added covering he didn't ask for; otherwise the model's richer prompt.
    func withPrompts(userText: String, useMyWords: Bool) -> Plan {
        let mine = userText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !mine.isEmpty else { return self }
        let said = mine.lowercased()
        func fix(_ prompt: String) -> String {
            if useMyWords { return mine }
            let p = prompt.lowercased()
            let added = Self.coveringWords.contains { p.range(of: "\\b\($0)\\b", options: .regularExpression) != nil
                                                      && said.range(of: "\\b\($0)\\b", options: .regularExpression) == nil }
            return added ? mine : prompt
        }
        var out = self
        out.actions = actions.map { a in
            switch a {
            case .make(let p): return .make(fix(p))
            case .redraw(let p, let t): return .redraw(fix(p), t)
            case .video(let p, let photo): return .video(fix(p), fromPhoto: photo)
            default: return a
            }
        }
        return out
    }

    /// True when the user's words ask for a picture to be made. Used to catch a brain that answers a plain
    /// question ("list the state capitals") by drawing it.
    static func asksForPicture(_ text: String) -> Bool {
        // Requests without a "picture word" ("a woman on a beach, nothing on") came back as text: the tools weren't
        // offered, or the question guard below forced text. Request words count too (the owner, 11-16).
        let pattern = RemoteConfig.string("picture_pattern") ?? #"\b(images?|pictures?|pics?|photos?|drawings?|paintings?|sketch(es)?|illustrations?|artwork|wallpapers?|logos?|icons?|posters?|renders?|selfies?|portraits?|cartoons?|memes?|stickers?|videos?|clips?|animations?|gifs?|draw|paint|illustrate|animate|visuali[sz]e|nsfw|nudes?|naked|topless|lingerie)\b"#
        return text.range(of: pattern, options: [.regularExpression, .caseInsensitive]) != nil
    }

    /// "Another one" / "one more" / "again" right after a picture.
    static func asksForAnother(_ text: String, after messages: [Message]) -> Bool {
        // The last two are this turn (my message and the reply being written); look at what came before them.
        guard let last = messages.dropLast(2).last(where: { $0.role == .fixer }),
              last.image != nil || last.video != nil else { return false }
        let pattern = #"\b(another( one)?|one more|again|more like (this|that)|try again|redo)\b"#
        return text.range(of: pattern, options: [.regularExpression, .caseInsensitive]) != nil
    }

    /// True for a plain question ("what are the 50 state capitals?"), the case the make-only guard exists for.
    static func isQuestion(_ text: String) -> Bool {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if t.hasSuffix("?") { return true }
        let pattern = RemoteConfig.string("question_pattern") ?? #"^(what|who|when|where|why|how|which|whose|is|are|was|were|do|does|did|can|could|should|would|will|list|name|explain|tell me)\b"#
        return t.range(of: pattern, options: [.regularExpression, .caseInsensitive]) != nil
    }

    /// True when the words could need the web (a question, or news/prices/weather/"look up"...). A search the brain
    /// asks for on anything else ("Test", "hi", "write me a poem") is refused and it answers directly.
    static func mayNeedWeb(_ text: String) -> Bool {
        if isQuestion(text) { return true }
        let pattern = RemoteConfig.string("web_pattern") ?? #"\b(news|latest|recent|current|today|tonight|tomorrow|yesterday|this (week|weekend|month|year)|price|prices|cost|weather|forecast|score|scores|won|winner|results?|release|released|schedule|open now|hours|near me|search|look (it |that |this )?up|google|find out|20[2-9]\d)\b"#
        return text.range(of: pattern, options: [.regularExpression, .caseInsensitive]) != nil
    }


    /// The first web search the brain asked for, if any.
    var searchQuery: String? {
        for a in actions { if case .search(let q) = a { return q } }
        return nil
    }

    /// Tolerant parser: finds the first JSON object in the reply; plain text becomes just a reply.
    static func parse(_ raw: String) -> Plan {
        guard let start = raw.firstIndex(of: "{"), let end = raw.lastIndex(of: "}"), start < end,
              let data = String(raw[start...end]).data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return Plan(say: raw.trimmingCharacters(in: .whitespacesAndNewlines), actions: [])
        }
        // A reply with prose and some other JSON in it (a sample config, say) is just an answer: keep all of it.
        if obj["say"] == nil, obj["actions"] == nil, obj["tool"] == nil {
            return Plan(say: raw.trimmingCharacters(in: .whitespacesAndNewlines), actions: [])
        }
        let say = obj["say"] as? String ?? ""
        var actions: [Action] = []
        // The brain sometimes sends one bare action ({"tool": "make", ...}) instead of {"say", "actions": [...]}.
        let list = obj["actions"] as? [[String: Any]] ?? (obj["tool"] != nil ? [obj] : [])
        for a in list {
            let targets = (a["targets"] as? [[String: Any]] ?? []).map(target)
            let prompt = a["prompt"] as? String ?? ""
            switch (a["tool"] as? String ?? "").lowercased() {
            case "remove", "erase": actions.append(.remove(targets))
            case "cutout", "cut_out", "cut out": actions.append(.cutout(targets))
            case "redraw", "edit", "inpaint": actions.append(.redraw(prompt, targets))
            case "upscale", "enhance": actions.append(.upscale)
            case "make", "generate": if !prompt.isEmpty { actions.append(.make(prompt)) }
            case "video", "animate", "make_video":
                let fromPhoto = a["from_photo"] as? Bool ?? (a["tool"] as? String == "animate")
                if !prompt.isEmpty || fromPhoto { actions.append(.video(prompt, fromPhoto: fromPhoto)) }
            case "queue_cancel", "cancel_job":
                let ids = (a["ids"] as? [String]) ?? (a["id"] as? String).map { [$0] } ?? []
                if !ids.isEmpty { actions.append(.queueCancel(ids)) }
            case "queue_move", "move_job":
                if let id = a["id"] as? String { actions.append(.queueMove(id, to: max(1, (a["to"] as? NSNumber)?.intValue ?? 1))) }
            case "stop_all", "stop_everything": actions.append(.stopAll)
            case "free_memory": actions.append(.freeMemory)
            case "search", "web_search", "websearch":
                let q = (a["query"] as? String ?? prompt).trimmingCharacters(in: .whitespacesAndNewlines)
                if !q.isEmpty { actions.append(.search(q)) }
            default: break
            }
        }
        var plan = Plan(say: say, actions: actions)
        plan.lineUp = list.contains { $0["line_up"] as? Bool == true }
        plan.explicit = list.compactMap { $0["explicit"] as? Bool }.first
        return plan
    }

    private static func target(_ t: [String: Any]) -> Target {
        let label = t["label"] as? String ?? ""
        let nums = (t["box"] as? [Any] ?? t["bbox_2d"] as? [Any] ?? []).compactMap { ($0 as? NSNumber)?.doubleValue }
        guard nums.count == 4 else { return Target(label: label, box: nil) }
        let x1 = min(nums[0], nums[2]) / 1000, x2 = max(nums[0], nums[2]) / 1000
        let y1 = min(nums[1], nums[3]) / 1000, y2 = max(nums[1], nums[3]) / 1000
        let r = CGRect(x: x1, y: y1, width: x2 - x1, height: y2 - y1).intersection(CGRect(x: 0, y: 0, width: 1, height: 1))
        return Target(label: label, box: r.isNull || r.width <= 0 || r.height <= 0 ? nil : r)
    }
}
