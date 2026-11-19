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
        }.joined(separator: ";")
    }

    var changeFormSignature: String { [changeText, changeMode, editor].joined(separator: "|") }

    // MARK: models

    /// The model the Model tile picks. Picking one makes the dock a "new picture" form again.
    var modelChoice: Binding<String> {
        Binding(get: { recipeID }, set: { recipeID = $0; target = .new })
    }

    var shapeOptions: [String] { recipe?.controls.first { $0.kind == "shape" }?.options ?? ["square", "portrait", "landscape", "wallpaper"] }

    var countControl: CreateControl? { recipe?.controls.first { $0.key == "count" } }

    // MARK: recipes

    /// The recipe row. The everyday recipes are always there; the explicit ones are added after them, as their own
    /// chips, only while "Explicit options in Make" is on in Settings (11-18: additive, not a mode swap).
    var visibleStyles: [StyleRecipe] {
        guard let c = recipeStore.catalog else { return [] }
        let kind = mode == "video" ? "video" : "picture"
        let all = c.recipes.filter { $0.kind == kind }
        let clean = all.filter { !$0.explicit }
        guard makeExplicit else { return clean }
        return clean + all.filter { $0.explicit }
    }

    /// Quick-pick rows for the recipe in use (or the usual ones before any recipe was used).
    var activeGroups: [QuickGroup] {
        guard let c = recipeStore.catalog else { return [] }
        let defaults = mode == "video" ? ["motion"] : ["subject", "hair", "outfit", "setting", "lighting"]
        let ids = applied?.kind == (mode == "video" ? "video" : "picture") ? (appliedChips.isEmpty ? defaults : appliedChips) : defaults
        return ids.compactMap { id in c.groups.first { $0.id == id } }
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
            if recipes.contains(where: { $0.id == s.model }) {
                recipeID = s.model
            } else if !s.model.isEmpty {
                styleNote = "That recipe's model isn't on your computer."
            }
            if let sh = s.shape, shapeOptions.contains(sh) { shape = sh }
            if let r = recipe {
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

    // MARK: switches

    func explicitSwitchChanged(_ on: Bool) {
        guard !on else { return }                  // turning it on changes nothing by itself: the recipe you pick sets things
        rules.explicitVideo = "off"
        explicitAddon = "Off"
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

    func tapQuick(_ option: String) {
        let b = quickBinding
        b.wrappedValue = PhraseList.toggled(b.wrappedValue, option)
        quickTick += 1
    }
}
