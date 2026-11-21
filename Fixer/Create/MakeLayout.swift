import SwiftUI
import UIKit

/// Make, "focus mode": the picture fills the room above a short panel, with a roll of this session's pictures under
/// it; the settings are pages in a drawer the panel opens. This file holds the layout and the small rules every part
/// shares (what is running, what the prompt is, what a recipe sets).
extension CreateScreen {
    // MARK: layout

    var unpairedCard: some View {
        VStack(spacing: 14) {
            Image(systemName: "desktopcomputer").font(.system(size: 34)).foregroundStyle(Fx.secondaryText)
            Text("Make runs on your computer, and this iPhone isn't connected to one yet.")
                .font(.headline)
                .multilineTextAlignment(.center)
            Text("Open Fixer Desktop on your computer, choose Pair a Phone, and scan the code in Settings.")
                .font(.footnote)
                .foregroundStyle(Fx.secondaryText)
                .multilineTextAlignment(.center)
            Button("Go to Settings") { router.tab = .settings }
                .buttonStyle(.borderedProminent)
                .frame(minHeight: 44)
        }
        .padding(24)
        .frame(maxWidth: 420)
    }

    /// The still pictures made this session, oldest first (videos left out).
    var sessionStills: [CreateResult] { store.results.filter { $0.videoURL == nil } }

    /// A picture made this session becomes the photo the next video starts from, straight from memory: it never has
    /// to be saved to the phone's Photos first. From the picture side this also switches Make to Video.
    func useForVideo(_ r: CreateResult) {
        guard r.videoURL == nil else { return }
        videoPhoto = r.image
        videoPhotoID = r.libraryID
        if mode != "video" { mode = "video" }
    }

    var createRunning: Bool { running != nil }
    var changeRunning: Bool { store.reviseStatus != nil }
    var anyRunning: Bool { createRunning || changeRunning || store.videoRunning }

    /// Whether the dock can change the picture on the canvas (a still picture of this session).
    var canChange: Bool { mode == "picture" && working != nil && working?.videoURL == nil }
    var effectiveTarget: MakeTarget { canChange ? target : .new }

    /// What the computer says it is doing right now, for the pill.
    var runStatus: String? {
        if createRunning { return status ?? "Working…" }
        if changeRunning { return store.reviseStatus }
        if store.videoRunning { return store.videoStatus }
        return nil
    }

    var runProgress: Double {
        if createRunning { return progress }
        if changeRunning { return store.reviseProgress }
        if store.videoRunning { return store.videoProgress }
        return 0
    }

    /// A message about the last run, shown in the pill until it is tapped away.
    var idleNote: String? {
        guard let note = rawNote, note != noteDismissed else { return nil }
        return note
    }

    /// The last run's message before any "tapped away" is applied. A dismissal is forgotten when the message goes
    /// or a new run starts, so the same message shows again next time.
    var rawNote: String? {
        if let n = store.videoNote { return n }
        if let n = store.reviseNote { return n }
        if let b = store.blockedNote { return b }
        return styleNote
    }

    func stopWhatRuns() {
        stopTick += 1
        if createRunning {
            running?.cancel()
            Desktop.shared.cancel(slot: "make:create")
            status = "Stopping…"
        } else if changeRunning {
            store.cancelChange()
        } else if store.videoRunning {
            store.cancelVideo()
        }
    }

    /// The header's Stop all: everything of this screen, and everything else on the computer.
    func stopEverything() {
        stopTick += 1
        running?.cancel()
        store.cancelRevise()
        Desktop.shared.stopAll()
        status = "Stopping…"
    }

    var showStopAll: Bool { anyRunning || !chat.jobs.isEmpty }

    // MARK: the prompt

    private func joined(prefix: String, words: String, suffix: String) -> String {
        let head = [prefix, words].map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.joinedNonEmpty(" ")
        let tail = suffix.trimmingCharacters(in: .whitespacesAndNewlines)
        if tail.isEmpty { return head }
        return head.isEmpty ? tail : head + ", " + tail
    }

    /// The recipe's opening words, the owner's words, the recipe's closing words. A photo being edited gets the
    /// owner's words as they are (they are an instruction, not a description).
    var assembledPrompt: String {
        let words = texts["prompt"] ?? ""
        if photo != nil, photoMode == "edit" { return words.trimmingCharacters(in: .whitespacesAndNewlines) }
        return joined(prefix: stylePrefix, words: words, suffix: styleSuffix)
    }

    var assembledVideoPrompt: String {
        joined(prefix: videoPrefix, words: videoPrompt, suffix: videoSuffix)
    }

    /// The form as a string, to tell "Create instead" (something changed) from "Stop drawing" (same as running).
    var formSignature: String {
        let nums = numbers.keys.sorted().map { "\($0)=\(numbers[$0] ?? 0)" }.joined(separator: ",")
        let mens = menus.keys.sorted().map { "\($0)=\(menus[$0] ?? "")" }.joined(separator: ",")
        let others = texts.keys.filter { $0 != "prompt" }.sorted().map { "\($0)=\(texts[$0] ?? "")" }.joined(separator: ",")
        return [assembledPrompt, recipeID, shape, photo == nil ? "nophoto" : "photo", photoMode, String(strength), editor,
                nums, mens, others, String(fixedSeed ?? -1), loraSignature, chosen.sorted().joined(separator: ","),
                String(morePhotos.count)].joined(separator: "|")
    }

    var videoFormSignature: String {
        [assembledVideoPrompt, videoPhoto == nil ? "nophoto" : "photo", rules.explicitVideo,
         loraSignature].joined(separator: "|")
    }

    /// Each add-on in use with its strength and extra words, so changing any of them counts as a change.
    var loraSignature: String {
        activeLoras.map { p in
            let words = (loraExtra[p.name] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            return "\(p.name)@\(p.strength)+\(words)"
        }.joined(separator: ";") + "~" + offTriggersRaw
    }

    var changeFormSignature: String { [changeText, changeMode, editor].joined(separator: "|") }

    // MARK: models

    /// The model the Model tile picks. Picking one makes the dock a "new picture" form again.
    var modelChoice: Binding<String> {
        Binding(get: { recipeID }, set: { id in
            // 11-20: a style stays across a model switch (its words fit any model), re-based on the new model; an
            // 18+ style goes when the new model is an everyday one.
            let changed = id != recipeID
            if changed, let a = applied, a.kind != "video", isExplicitStyle(a.id),
               recipes.first(where: { $0.id == id })?.adult != true { removeStyle() }
            recipeID = id
            if changed { rebaseStyle() }
            target = .new
        })
    }

    var shapeOptions: [String] { recipe?.controls.first { $0.kind == "shape" }?.options ?? ["square", "portrait", "landscape", "wallpaper"] }

    var countControl: CreateControl? { recipe?.controls.first { $0.key == "count" } }

    // MARK: recipes

    /// The recipe row. The everyday recipes are always there; the explicit ones are added after them, as their own
    /// chips, only while "Explicit options in Make" is on in Settings (11-18: additive, not a mode swap).
    /// 11-20 (the owner: picking a style swapped the model and add-ons "from beneath me" and sat through a load): a
    /// style never changes the model (applyStyle). Every everyday style is offered on every model; the 18+ ones only
    /// on an 18+ model, while 18+ is on. (Showing only the model's own styles hid every everyday style on Chroma.)
    var visibleStyles: [StyleRecipe] {
        guard let c = recipeStore.catalog else { return [] }
        let kind = mode == "video" ? "video" : "picture"
        let all = c.recipes.filter { $0.kind == kind }
        let clean = all.filter { !$0.explicit }
        guard makeExplicit, kind == "video" || recipe?.adult == true else { return clean }
        // the current model's own 18+ styles first
        let adult = all.filter(\.explicit)
        return clean + adult.filter { $0.model == recipeID } + adult.filter { $0.model != recipeID }
    }

    /// Quick-pick rows for the recipe in use (or the usual ones before any recipe was used).
    var activeGroups: [QuickGroup] {
        guard let c = recipeStore.catalog else { return [] }
        // 11-19: framing (Camera), Pose and Face were missing until a style was picked; the owner looked for full
        // body / from behind / over the shoulder / focus and couldn't find them.
        let defaults = mode == "video" ? ["motion"]
            : ["subject", "hair", "outfit", "expression", "pose", "setting", "lighting", "camera"]
        var ids = applied?.kind == (mode == "video" ? "video" : "picture") ? (appliedChips.isEmpty ? defaults : appliedChips) : defaults
        // 11-20 (the owner: the 18+ quick picks were gone): they came only with an 18+ style, and with the 18+ styles
        // folded away nobody picked one. While 18+ is on, the 18+ rows are always there, after the style's own.
        if makeExplicit, mode != "video" {
            // 11-21: "Undress" leads the 18+ rows — the first thing wanted when working an uploaded photo with 18+ on.
            for id in ["undress", "body_size", "act", "anatomy_f", "anatomy_m", "fluids", "reactions", "kink", "fetish"]
            where !ids.contains(id) { ids.append(id) }
        }
        // 11-19: with 18+ off, a row that has only 18+ options (anatomy, kink, ...) isn't shown at all.
        return ids.compactMap { id in c.groups.first { $0.id == id } }
            .filter { makeExplicit || !$0.options.isEmpty }
    }

    private var appliedChips: [String] {
        guard let a = applied else { return [] }
        return recipeStore.catalog?.recipes.first { $0.id == a.id }?.chips ?? []
    }

    func currentSnapshot(_ a: AppliedStyle) -> StyleSnapshot {
        var snap = StyleSnapshot()
        if a.kind == "video" {
            snap.prefix = videoPrefix
            snap.suffix = videoSuffix
            snap.explicitVideo = rules.explicitVideo
            return snap
        }
        snap.recipeID = recipeID
        snap.shape = shape
        for k in a.numberKeys {
            if let c = recipe?.controls.first(where: { $0.key == k }) { snap.numbers[k] = numbers[k] ?? c.defaultNumber ?? c.min }
        }
        for k in a.menuKeys {
            if let c = recipe?.controls.first(where: { $0.key == k }) { snap.menus[k] = menus[k] ?? c.defaultText ?? c.options.first ?? "" }
        }
        snap.negative = texts["negative"] ?? ""
        snap.prefix = stylePrefix
        snap.suffix = styleSuffix
        return snap
    }

    /// Something a recipe set was changed by hand, or the recipe isn't in the row any more (the Explicit switch).
    var styleIsCustom: Bool {
        guard let a = applied else { return false }
        if !visibleStyles.contains(where: { $0.id == a.id }) { return true }
        return currentSnapshot(a) != a.snapshot
    }

    /// Taps a recipe: sets the model, shape, steps, guidance, sampler, schedule, shift, add-on, "leave out" and the
    /// opening and closing words. It never touches the owner's own words.
    func applyStyle(_ s: StyleRecipe) {
        target = .new
        styleNote = nil
        var numberKeys: [String] = []
        var menuKeys: [String] = []
        if s.kind == "video" {
            mode = "video"
            videoPrefix = s.prefix
            videoSuffix = s.suffix
            if let ev = s.explicitVideo { rules.explicitVideo = ev }
        } else {
            mode = "picture"
            // 11-20: the model stays as it is. A style made for this model also sets its steps, guidance, sampler and
            // "leave out"; on another model it brings only its words, quick-pick rows and shape (another model's
            // numbers would be wrong here).
            if let sh = s.shape, shapeOptions.contains(sh) { shape = sh }
            if let r = recipe, s.model == recipeID {
                for (k, v) in s.numbers {
                    guard let c = r.controls.first(where: { $0.key == k }), c.kind == "int" || c.kind == "float" else { continue }
                    numbers[k] = Swift.min(c.max, Swift.max(c.min, v))
                    numberKeys.append(k)
                }
                for (k, v) in s.menus {
                    guard let c = r.controls.first(where: { $0.key == k }), c.kind == "menu", c.options.contains(v) else { continue }
                    menus[k] = v
                    menuKeys.append(k)
                }
                if let neg = s.negative, !neg.isEmpty, r.controls.contains(where: { $0.key == "negative" }) {
                    texts["negative"] = neg
                }
            }
            stylePrefix = s.prefix
            styleSuffix = s.suffix
        }
        var a = AppliedStyle(id: s.id, kind: s.kind, numberKeys: numberKeys.sorted(), menuKeys: menuKeys.sorted(),
                             snapshot: StyleSnapshot())
        a.snapshot = currentSnapshot(a)
        applied = a
    }

    /// 11-19 (the owner: a picked style couldn't be tapped off). Lets go of the style in use: its opening and closing
    /// words go, and the settings it tuned (steps, guidance, sampler, "leave out") go back to the model's own. The
    /// model, the shape and the owner's own words stay.
    func removeStyle() {
        guard let a = applied else { return }
        styleNote = nil
        if a.kind == "video" {
            videoPrefix = ""; videoSuffix = ""
        } else {
            stylePrefix = ""; styleSuffix = ""
            for k in a.numberKeys { numbers[k] = nil }
            for k in a.menuKeys { menus[k] = nil }
            if !a.snapshot.negative.isEmpty, texts["negative"] == a.snapshot.negative { texts["negative"] = nil }
        }
        applied = nil
    }

    // MARK: switches

    /// 11-19: the models Make offers: the 18+ ones (Chroma, Juggernaut) only while 18+ is on.
    var shownRecipes: [CreateRecipe] { makeExplicit ? recipes : recipes.filter { !$0.adult } }

    /// 11-19 (the owner: "toggling 18+ should swap to explicit models and going to sfw should remove all explicit
    /// models and addons silently"). Turning 18+ on moves to an 18+ model; with 18+ off an 18+ model in use moves to
    /// the everyday one and every 18+ add-on is dropped from the picks. No notes: it just happens.
    func alignModelToExplicit(turnedOn: Bool) {
        guard !recipes.isEmpty else { return }
        if makeExplicit {
            if turnedOn, recipe?.adult != true,
               let r = recipes.first(where: { $0.adult && $0.id == "chroma" }) ?? recipes.first(where: \.adult) {
                swapModel(to: r.id)
            }
        } else {
            if recipe?.adult == true || recipe == nil, let r = recipes.first(where: { !$0.adult }) { swapModel(to: r.id) }
            let adultAddons = Set(loraLibrary.filter(\.explicit).map(\.file))
            if !adultAddons.isEmpty {
                loras.removeAll { adultAddons.contains($0.name) }
                videoLoras.removeAll { adultAddons.contains($0.name) }
            }
        }
    }

    /// A quiet model swap starts from the new model's own settings (one model's guidance or steps are wrong for
    /// another); how many pictures and the shape stay. A recipe in use that was for another model is let go.
    private func swapModel(to id: String) {
        guard id != recipeID else { return }
        let count = numbers["count"]
        numbers = [:]
        if let count { numbers["count"] = count }
        menus = [:]
        texts["negative"] = nil
        if let a = applied, a.kind != "video", a.numberKeys.count + a.menuKeys.count > 0 {
            applied = AppliedStyle(id: a.id, kind: a.kind, numberKeys: [], menuKeys: [], snapshot: a.snapshot)
        }
        recipeID = id
        rebaseStyle()                                  // 11-20: the style stays, on the new model's own settings
    }

    /// The style in use, measured again against the current model, so a model switch doesn't show it as "Custom".
    func rebaseStyle() {
        guard var a = applied, a.kind != "video" else { return }
        a.snapshot = currentSnapshot(a)
        applied = a
    }

    func isExplicitStyle(_ id: String) -> Bool {
        recipeStore.catalog?.recipes.first { $0.id == id }?.explicit == true
    }

    /// 11-19: 18+ off takes out what Make's own 18+ chips put in: the 18+ style tips, and the 18+ quick-pick
    /// phrases from the words (only whole phrases a quick pick adds, the way tapping it again would).
    func removeAdultPicks() {
        let adultTips = Set(recipes.flatMap { $0.toggles.flatMap(\.items) }.filter(\.explicit).map(\.id))
        if !adultTips.isDisjoint(with: chosen) { chosen.subtract(adultTips) }
        let groups = recipeStore.catalog?.groups ?? []
        let everyday = Set(groups.flatMap(\.options).map { $0.lowercased() })
        let phrases = groups.flatMap(\.explicitOptions).filter { !everyday.contains($0.lowercased()) }
        guard !phrases.isEmpty else { return }
        func strip(_ text: String) -> String {
            var t = text
            for p in phrases where PhraseList.contains(t, p) { t = PhraseList.toggled(t, p) }
            return t
        }
        let words = texts["prompt"] ?? ""
        let cleaned = strip(words)
        if cleaned != words { texts["prompt"] = cleaned }
        let v = strip(videoPrompt)
        if v != videoPrompt { videoPrompt = v }
        let c = strip(changeText)
        if c != changeText { changeText = c }
    }

    func explicitSwitchChanged(_ on: Bool) {
        alignModelToExplicit(turnedOn: on)
        guard !on else { return }                  // turning it on changes nothing else: the recipe you pick sets things
        removeAdultPicks()                         // only at the moment 18+ goes off, never on later edits
        // 11-19: an 18+ recipe in use loses its own opening and closing words (never the owner's), so the next run
        // doesn't carry them into an 18+-off session.
        if let a = applied, recipeStore.catalog?.recipes.first(where: { $0.id == a.id })?.explicit == true {
            if a.kind == "video" { videoPrefix = ""; videoSuffix = "" } else { stylePrefix = ""; styleSuffix = "" }
            applied = nil
        }
        rules.resetExplicitChoices()
        explicitAddon = "Off"
        if editor == "qwen-explicit" { editor = "auto" }
        if let c = recipe?.controls.first(where: { $0.key == "explicit" }), c.options.contains("Off") {
            menus["explicit"] = "Off"
        }
    }


    // MARK: quick picks

    /// The words a quick pick edits: the change box when changing, else the video or picture words.
    var quickBinding: Binding<String> {
        if effectiveTarget == .change { return $changeText }
        if mode == "video" { return $videoPrompt }
        return Binding(get: { texts["prompt"] ?? "" }, set: { texts["prompt"] = $0 })
    }

    /// 11-21: a quick pick's emphasis, stepped 0.1 at a time between 0.8 and 1.5 (1.0 = plain words, no brackets).
    func nudgeQuick(_ option: String, by delta: Double) {
        let b = quickBinding
        guard PhraseList.contains(b.wrappedValue, option) else { return }
        let now = PhraseList.weight(b.wrappedValue, option)
        let next = Swift.min(1.5, Swift.max(0.8, ((now + delta) * 10).rounded() / 10))
        b.wrappedValue = PhraseList.setWeight(b.wrappedValue, option, next)
        quickTick += 1
    }

    /// 11-21: "Use in Make" from Settings → Macros. Loads the macro into the picture form: its words are added to
    /// yours, its add-ons switched on (only ones installed and fitting this model), its settings and model applied.
    func applyPendingMacro() {
        guard let m = router.useMacro, !loading, !recipes.isEmpty else { return }
        router.useMacro = nil
        mode = "picture"
        target = .new
        var skipped: [String] = []
        if m.explicit, !makeExplicit { rules.explicitNow = true }
        if let id = m.model, !id.isEmpty {
            if shownRecipes.contains(where: { $0.id == id }) { modelChoice.wrappedValue = id }
            else { skipped.append("its model") }
        }
        let add = m.promptAdd.trimmingCharacters(in: .whitespacesAndNewlines)
        if !add.isEmpty {
            let cur = (texts["prompt"] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if !cur.localizedCaseInsensitiveContains(add) {
                texts["prompt"] = cur.isEmpty ? add : (cur.hasSuffix(",") ? cur + " " + add : cur + ", " + add)
            }
        }
        let neg = m.negativeAdd.trimmingCharacters(in: .whitespacesAndNewlines)
        if !neg.isEmpty, recipe?.controls.contains(where: { $0.key == "negative" }) == true {
            let cur = (texts["negative"] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            if !cur.localizedCaseInsensitiveContains(neg) {
                texts["negative"] = cur.isEmpty ? neg : (cur.hasSuffix(",") ? cur + " " + neg : cur + ", " + neg)
            }
        }
        for (key, value) in [("cfg", m.cfg), ("steps", m.steps)] {
            guard let value, let c = recipe?.controls.first(where: { $0.key == key }) else { continue }
            numbers[key] = Swift.min(c.max, Swift.max(c.min, value))
        }
        let fitting = Set(fittingLoras.map(\.file))
        for l in m.loras {
            guard fitting.contains(l.file) else { skipped.append("an add-on"); continue }
            loras.removeAll { $0.name == l.file }
            loras.append(LoraPick(name: l.file, strength: l.strength))
        }
        let name = m.name.isEmpty ? "Macro" : m.name
        macroResult = skipped.isEmpty ? "“\(name)” is loaded in Make."
            : "“\(name)” is loaded in Make, without " + Set(skipped).sorted().joined(separator: " or ")
              + " (not available here)."
    }

    func tapQuick(_ option: String) {
        let b = quickBinding
        b.wrappedValue = PhraseList.toggled(b.wrappedValue, option)
        quickTick += 1
    }
}
