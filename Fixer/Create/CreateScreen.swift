import AVFoundation
import Photos
import PhotosUI
import SwiftUI
import UIKit

// MARK: - Recipes (sent by the computer; built from ComfyUI's own option lists)

struct CreateControl: Identifiable {
    let key: String
    let label: String
    let kind: String            // text, shape, int, float, menu, seed
    let options: [String]
    let min: Double, max: Double, step: Double
    let defaultNumber: Double?
    let defaultText: String?
    let advanced: Bool
    let help: String?
    let left: String?           // what the low end of a slider does
    let right: String?          // what the high end does
    var id: String { key }

    init?(_ d: [String: Any]) {
        guard let key = d["key"] as? String, let kind = d["kind"] as? String else { return nil }
        self.key = key
        self.kind = kind
        label = d["label"] as? String ?? key
        options = d["options"] as? [String] ?? []
        min = (d["min"] as? NSNumber)?.doubleValue ?? 0
        max = (d["max"] as? NSNumber)?.doubleValue ?? 1
        step = (d["step"] as? NSNumber)?.doubleValue ?? 1
        defaultNumber = (d["default"] as? NSNumber)?.doubleValue
        defaultText = d["default"] as? String
        advanced = d["advanced"] as? Bool ?? false
        help = d["help"] as? String
        left = d["left"] as? String
        right = d["right"] as? String
    }
}

struct PromptToggle: Identifiable, Hashable { let id: String; let label: String }
struct PromptToggleGroup: Identifiable { let name: String; let items: [PromptToggle]; var id: String { name } }

struct CreateRecipe: Identifiable {
    let id: String
    let name: String
    let kind: String
    let controls: [CreateControl]
    let toggles: [PromptToggleGroup]
    let model: String?          // the phone-side model id, when the computer sends it
    let note: String?
    let loraFamily: String?     // which LoRAs fit this model (sdxl, chroma, qwen21); nil from an older computer

    init?(_ d: [String: Any]) {
        guard let id = d["id"] as? String else { return nil }
        self.id = id
        model = d["model"] as? String
        note = d["note"] as? String
        loraFamily = d["lora_family"] as? String
        name = d["name"] as? String ?? id
        kind = d["kind"] as? String ?? "image"
        controls = (d["controls"] as? [[String: Any]] ?? []).compactMap(CreateControl.init)
        toggles = (d["toggles"] as? [[String: Any]] ?? []).map { g in
            PromptToggleGroup(name: g["name"] as? String ?? "",
                              items: (g["items"] as? [[String: Any]] ?? []).compactMap { it in
                                  guard let id = it["id"] as? String else { return nil }
                                  return PromptToggle(id: id, label: it["label"] as? String ?? id)
                              })
        }
    }
}

enum CreateEvent {
    case status(String), progress(Double), preview(UIImage)
    /// A finished picture; `enhanced` is the prompt the computer wrote from the owner's words, when Enhance was on.
    case image(UIImage, seed: Int, enhanced: String?)
}

struct CreateResult: Identifiable {
    let id = UUID()
    let image: UIImage
    let seed: Int
    var recipeID = ""
    var texts: [String: String] = [:]       // the form as it was: prompt, leave-out...
    var numbers: [String: Double] = [:]
    var menus: [String: String] = [:]
    var shape = "square"
    var toggles: [String] = []              // prompt helpers that were on
    var note: String?                       // "Revised: ..." for a revision
    var libraryID: UUID?                    // its copy in the Library
    var videoURL: URL?                      // set for a video: `image` is then its first frame
    var explicit: String?                   // a video's Explicit setting: off, on or auto
    var enhanced: String?                   // the prompt the computer wrote from my words (Enhance on)
}

/// Pictures made on the Create screen: in memory only (never written to disk); "Clear everything" empties it.
@MainActor
final class CreateStore: ObservableObject {
    static let shared = CreateStore()
    @Published var results: [CreateResult] = []
    /// "1 picture blocked by your content filter": shown on the Make screen after a run.
    @Published var blockedNote: String?
    /// A revision in progress ("Revising…"), a message about the last one, and the result it added (the viewer moves to it).
    @Published var reviseStatus: String?
    @Published var reviseNote: String?
    @Published var revisedResultID: UUID?
    /// The change being made: which picture, the computer's live preview of the new one, and how far along it is.
    @Published var reviseBaseID: UUID?
    @Published var revisePreview: UIImage?
    @Published var reviseProgress: Double = 0
    /// Bumped by `clear()`, so a run that was still going when Clear everything ran adds nothing afterwards.
    private(set) var epoch = 0
    fileprivate var revising: Task<Void, Never>?
    /// A video being made (from the Video form, or "Animate" in the viewer): status, progress and a message.
    @Published var videoStatus: String?
    @Published var videoProgress: Double = 0
    @Published var videoNote: String?
    fileprivate var videoJob: Task<Void, Never>?
    /// 11-17: which task is the current one, so a replaced (older) task never clears the newer one's status when it ends.
    fileprivate var videoToken: UUID?
    fileprivate var reviseToken: UUID?
    var videoRunning: Bool { videoStatus != nil }

    init() {
        Self.sweepTempVideos()
        // Finished work the computer kept while Fixer was closed is filed here.
        JobInbox.shared.toMake = { [weak self] kind, words, frames in
            Task { await self?.adopt(kind: kind, words: words, frames: frames) }
        }
    }

    /// Videos made in Make wait in the temp folder (so they can play); they never outlive the app run.
    private static func sweepTempVideos() {
        let dir = FileManager.default.temporaryDirectory
        let names = (try? FileManager.default.contentsOfDirectory(atPath: dir.path)) ?? []
        for n in names where n.hasPrefix("make-") && n.hasSuffix(".mp4") {
            try? FileManager.default.removeItem(at: dir.appendingPathComponent(n))
        }
    }

    func clear() {
        epoch += 1
        revising?.cancel(); revising = nil
        videoJob?.cancel(); videoJob = nil
        for r in results { if let u = r.videoURL { try? FileManager.default.removeItem(at: u) } }
        results = []; blockedNote = nil; reviseStatus = nil; reviseNote = nil; revisedResultID = nil
        reviseBaseID = nil; revisePreview = nil; reviseProgress = 0
        videoStatus = nil; videoProgress = 0; videoNote = nil
    }
    func noteBlocked(_ what: String = "picture") { blockedNote = "1 \(what) blocked by your content filter." }
    /// 11-17: stops this screen's own changes and video on the computer (by place), not everything there.
    func cancelRevise() { cancelChange(); cancelVideo() }
    func cancelChange() { revising?.cancel(); Desktop.shared.cancel(slot: "make:change") }
    func cancelVideo() { videoJob?.cancel(); Desktop.shared.cancel(slot: "make:video") }

    /// Makes a video on the computer, from words and/or a start photo, checks it with the content filter and adds it to
    /// this session's results and the Library. One at a time. Used by the Video form and by "Animate" in the viewer.
    func startVideo(prompt: String, start: UIImage?, explicit: String, parent: UUID?, action: String,
                   style: String? = nil, loras: [LoraSetting] = []) {
        // 11-17: a new video from here replaces the unfinished one (the computer drops the old one itself).
        var cg: CGImage?
        if let start {
            cg = ImageKit.cgImage(start)
            if cg == nil { videoNote = "Couldn't read that photo."; return }
        }
        let typed = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        let sent = typed.isEmpty && cg != nil ? "natural, gentle motion" : typed
        guard !sent.isEmpty else { videoNote = "Say what the video should show."; return }
        let myEpoch = epoch
        let filter = cg == nil ? Rules.shared.newFilter : Rules.shared.editFilter
        let startPhoto = cg
        videoJob?.cancel()
        let mine = UUID()
        videoToken = mine
        videoStatus = "Starting…"
        videoProgress = 0
        videoNote = nil
        blockedNote = nil
        videoJob = Task {
            do {
                let data = try await Desktop.shared.video(
                    prompt: sent, start: startPhoto, explicit: explicit, slot: "make:video", style: style,
                    loras: loras,
                    status: { msg in Task { @MainActor in if myEpoch == self.epoch { self.videoStatus = msg } } },
                    progress: { v in Task { @MainActor in if myEpoch == self.epoch { self.videoProgress = v } } })
                if await ContentGate.blocksVideo(data, filter) {
                    if myEpoch == epoch { noteBlocked("video"); videoNote = blockedNote }
                } else if myEpoch == epoch, !Task.isCancelled {
                    await addVideo(data, words: typed, explicit: explicit, parent: parent, action: action, epoch: myEpoch)
                }
            } catch is CancellationError {
            } catch PCError.stopped(_) {
            } catch {
                if myEpoch == epoch, videoToken == mine { videoNote = error.localizedDescription }
            }
            if myEpoch == epoch, videoToken == mine { videoStatus = nil; videoJob = nil }
        }
    }

    /// Finished work the computer held while Fixer was closed, filed into Make (11-17).
    func adopt(kind: String, words: String, frames: [[String: Any]]) async {
        let myEpoch = epoch
        let filter = (kind == "edit" || kind == "change" || kind == "upscale") ? Rules.shared.editFilter : Rules.shared.newFilter
        for fr in frames {
            guard let b64 = fr["v"] as? String, let data = Data(base64Encoded: b64) else { continue }
            switch fr["t"] as? String {
            case "image":
                guard let ui = UIImage(data: data), let cg = ui.cgImage else { continue }
                if await ContentGate.blocks(cg, filter) { if myEpoch == epoch { noteBlocked() }; continue }
                guard myEpoch == epoch else { return }
                let info = Made(source: .make, action: (kind == "edit" || kind == "change") ? "edit" : "make",
                                words: words.isEmpty ? nil : words)
                let id = LibraryStore.shared.add(picture: ui, meta: info.meta(kind: .picture, conversation: nil))
                results.append(CreateResult(image: ui, seed: (fr["seed"] as? NSNumber)?.intValue ?? 0,
                                            texts: ["prompt": words], libraryID: id))
            case "video":
                if await ContentGate.blocksVideo(data, filter) { if myEpoch == epoch { noteBlocked("video") }; continue }
                await addVideo(data, words: words, explicit: "auto", parent: nil, action: "video", epoch: myEpoch)
            default: break
            }
        }
    }

    private func addVideo(_ data: Data, words: String, explicit: String, parent: UUID?, action: String, epoch myEpoch: Int) async {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("make-\(UUID().uuidString).mp4")
        do { try data.write(to: url, options: [.atomic, .completeFileProtectionUnlessOpen]) }
        catch { videoNote = "Couldn't keep the video."; return }
        let frame = await Self.firstFrame(url)
        guard myEpoch == epoch else { try? FileManager.default.removeItem(at: url); return }   // Clear everything ran meanwhile
        var info = Made(source: .make, action: action, words: words.isEmpty ? nil : words)
        info.model = Rules.shared.pcVideoID
        info.explicitVideo = explicit
        info.parent = parent
        let libraryID = LibraryStore.shared.addVideo(data, meta: info.meta(kind: .video, conversation: nil), source: url)
        let made = CreateResult(image: frame, seed: 0, texts: ["prompt": words], note: action == "animate" ? "Animated" : nil,
                                libraryID: libraryID, videoURL: url, explicit: explicit)
        results.append(made)
        revisedResultID = made.id
    }

    private static func firstFrame(_ url: URL) async -> UIImage {
        let gen = AVAssetImageGenerator(asset: AVURLAsset(url: url))
        gen.appliesPreferredTrackTransform = true
        gen.maximumSize = CGSize(width: 800, height: 800)
        if let cg = try? await gen.image(at: .zero).image { return UIImage(cgImage: cg) }
        return UIGraphicsImageRenderer(size: CGSize(width: 16, height: 9)).image { c in
            UIColor.black.setFill(); c.fill(CGRect(x: 0, y: 0, width: 16, height: 9))
        }
    }

    /// Changes a picture from this session and adds the result next to the original. With a painted spot the picture's
    /// own model redraws only that spot; without one, "edit" lets the photo editor change just what was asked and keep the
    /// rest, and "redraw" has the picture's own model redraw it with the change added to its words. `explicit` is the
    /// Explicit add-on switch to use for a Qwen-Image 2.1 edit (nil = the switch the picture was made with).
    func revise(baseID: UUID, change: String, mode requested: String, mask: CGImage?, strength: Double?,
                editor: String = "auto", explicit: String? = nil) {
        guard let base = results.first(where: { $0.id == baseID }), let cg = base.image.cgImage,
              let jpeg = Desktop.jpeg(cg, longest: 1536, quality: 0.92) else { return }
        let change = change.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !change.isEmpty else { return }
        let original = base.texts["prompt"] ?? ""
        let mode = mask != nil ? "paint" : requested
        var values: [String: Any] = ["image": jpeg, "count": 1, "shape": base.shape, "loras": [[String: Any]]()]
        if mode == "paint" {
            guard let mask, ImageKit.maskBounds(mask) != nil, let data = Desktop.png(mask) else { return }
            for (k, v) in base.texts where k != "prompt" { values[k] = v }
            for (k, v) in base.menus { values[k] = v }
            for (k, v) in base.numbers where k != "count" { values[k] = v }
            values["toggles"] = base.toggles
            values["prompt"] = original
            values["change"] = change
            values["mask"] = data
            values["image_mode"] = "paint"
            if let strength { values["strength"] = strength }
        } else if mode == "edit" {
            values["prompt"] = change
            values["image_mode"] = "edit"
            values["editor"] = editor
            values["explicit"] = explicit ?? base.menus["explicit"] ?? "Auto"
        } else {
            for (k, v) in base.texts where k != "prompt" { values[k] = v }   // keep "Leave out" and the like
            values["toggles"] = base.toggles
            values["prompt"] = original.isEmpty ? change : "\(original). \(change)"
            values["image_mode"] = "start"
            values["strength"] = 0.55
            for (k, v) in base.menus { values[k] = v }
            for (k, v) in base.numbers where k != "count" { values[k] = v }
        }
        // "Enhance my prompt" for changes (off unless the owner turned it on beside the change box); a painted spot
        // is described by its own few words and is never enhanced.
        if mode != "paint", UserDefaults.standard.object(forKey: "make.enhanceEdits") as? Bool ?? false {
            values["enhance"] = true
        }
        let payload = values
        let spot = mask
        let madePrompt = values["prompt"] as? String ?? original
        let myEpoch = epoch
        // 11-17: a new change from here replaces the unfinished one (the computer drops the old one itself).
        revising?.cancel()
        let mine = UUID()
        reviseToken = mine
        reviseStatus = "Changing it…"
        reviseNote = nil
        reviseBaseID = baseID
        revisePreview = nil
        reviseProgress = 0
        revising = Task {
            do {
                try await Desktop.shared.create(recipe: base.recipeID, values: payload, slot: "make:change") { event in
                    Task { @MainActor in
                        switch event {
                        case .status(let st): self.reviseStatus = st
                        case .progress(let v): if myEpoch == self.epoch { self.reviseProgress = v }
                        case .preview(let ui): if myEpoch == self.epoch { self.revisePreview = ui }
                        case .image(var ui, let seed, let enhanced):
                            var texts = base.texts
                            if mode == "redraw" { texts["prompt"] = madePrompt }
                            if let spot, let patch = ui.cgImage, let orig = base.image.cgImage {
                                // Keep every pixel outside the spot exactly as it was (the copy sent was compressed).
                                let wide = ImageKit.softened(spot, grow: 56, feather: 4)
                                if let kept = ImageKit.blend(base: orig, patch: patch, mask: wide) {
                                    ui = UIImage(cgImage: kept)
                                }
                            }
                            if let cg = ui.cgImage, await ContentGate.blocks(cg, Rules.shared.editFilter) {
                                if myEpoch == self.epoch { self.reviseNote = "1 picture blocked by your content filter." }
                                return
                            }
                            guard myEpoch == self.epoch else { return }          // Clear everything ran meanwhile
                            var meta = Made(source: .make, action: "change", words: change, model: base.recipeID,
                                            recipeID: base.recipeID, choice: base.recipeID, seed: seed,
                                            parent: base.libraryID).meta(kind: .picture, conversation: nil)
                            meta.texts = texts
                            meta.numbers = base.numbers
                            meta.menus = base.menus
                            meta.shape = base.shape
                            meta.toggles = base.toggles
                            meta.imageMode = mode
                            meta.enhanced = enhanced
                            let libraryID = LibraryStore.shared.add(picture: ui, meta: meta)
                            let made = CreateResult(image: ui, seed: seed, recipeID: base.recipeID, texts: texts,
                                                    numbers: base.numbers, menus: base.menus, shape: base.shape,
                                                    toggles: base.toggles, note: "Changed: \(change)", libraryID: libraryID,
                                                    enhanced: enhanced)
                            self.results.append(made)
                            self.revisedResultID = made.id
                        }
                    }
                }
            } catch is CancellationError {
            } catch PCError.stopped(_) {
            } catch {
                if myEpoch == epoch, reviseToken == mine { reviseNote = error.localizedDescription }
            }
            if myEpoch == epoch, reviseToken == mine {
                reviseStatus = nil; revising = nil; reviseBaseID = nil; revisePreview = nil; reviseProgress = 0
            }
        }
    }

    /// How a change is made by default: the explicit-capable picture models redraw (so the change stays in their own
    /// look); the others let the photo editor keep everything but what was asked.
    static func defaultChangeMode(for recipeID: String) -> String {
        ["juggernaut", "chroma"].contains(recipeID) ? "redraw" : "edit"
    }
}

// MARK: - The screen

/// Make pictures with full control: a recipe, your exact words (no chat model in between), shape, how many, LoRAs,
/// and every ComfyUI setting under Advanced as menus and sliders.
struct CreateScreen: View {
    @ObservedObject var chat: Chat
    @ObservedObject var store = CreateStore.shared
    @ObservedObject var desktop = Desktop.shared
    @ObservedObject var rules = Rules.shared
    @EnvironmentObject var router: AppRouter

    @State var recipes: [CreateRecipe] = []
    @State var loraFiles: [String] = []
    @State var recipeID = ""
    @State var texts: [String: String] = [:]
    @State var numbers: [String: Double] = [:]
    @State var menus: [String: String] = [:]
    @State var shape = "square"
    @State var fixedSeed: Int?
    @State var loras: [LoraPick] = []
    @State var videoLoras: [LoraPick] = []          // the add-ons switched on for videos (pictures keep their own)
    @State var loraLibrary: [LoraEntry] = []        // every LoRA on the computer, with the model family each fits
    @State var videoLoraFamily = "wan22"
    @State var showLoraBrowser = false
    @State var loading = true
    @State var failed: String?
    @State var running: Task<Void, Never>?
    @State var runToken: UUID?                 // 11-17: which run is the current one (a replaced run doesn't clear it)
    @State var showQueue = false               // 11-17: what the computer is making and what waits
    @State var status: String?
    @State var progress: Double = 0
    @State var preview: UIImage?
    @State var viewing: CreateResult?
    @State var photo: UIImage?                 // a photo to edit, start from, or use as a reference
    @State var photoPick: PhotosPickerItem?
    @AppStorage("make.editor") var editor = "auto"   // which editor changes a photo (11-17)
    @AppStorage("make.explicitAddon") var explicitAddon = "Auto"   // 11-17: Qwen-Image 2.1 add-on for photo edits
    @AppStorage("make.enhance") var enhanceNew = true              // 11-17: Enhance my prompt, new pictures
    @AppStorage("make.enhanceEdits") var enhanceEdits = false      // ... and changes to a photo or picture
    @State var morePhotos: [UIImage] = []      // photos 2 and 3 for an edit that combines photos
    @State var morePick: PhotosPickerItem?
    @State var photoMode = "edit"              // edit, start, reference
    @State var strength = 0.6                  // "start": how much of the photo gets redrawn
    @State var chosen: Set<String> = []        // prompt helpers switched on
    @State var showHelpers = false
    @State var showModels = false
    @State var mode = "picture"                // picture or video
    @State var videoPrompt = ""
    @State var videoPhoto: UIImage?            // the photo a video starts from ("Animate this photo")
    @State var videoPhotoID: UUID?             // its copy in the Library
    @State var videoPhotoPick: PhotosPickerItem?
    @State var showVideoPhotos = false         // the Photos picker for a video's start photo (opened from the Photo tile's menu)
    @State var workingID: UUID?                // the picture the change bar works on (nil = the newest)
    @State var changeText = ""
    @State var changeMode = "edit"             // edit: keep the rest. redraw: the picture's own model again

    // The focus layout (11-17): recipes, quick picks, the panel and what has the keyboard.
    @ObservedObject var recipeStore = MakeRecipeStore.shared
    @EnvironmentObject var keyboard: KeyboardObserver
    @AppStorage("make.explicit") var makeExplicit = false              // swaps in the explicit recipes, chips and models
    @State var panelOpen = false                                       // the settings drawer is up (closed each time Make opens)
    @AppStorage("make.section") var savedSection = MakeSection.photo.rawValue   // the drawer page last looked at
    @FocusState var focus: MakeField?
    @State var target = MakeTarget.new                 // new: the form makes a picture. change: it changes the one on the canvas
    @State var stylePrefix = ""                        // a recipe's opening words, shown and editable
    @State var styleSuffix = ""                        // ... and its closing words (in the drawer)
    @State var videoPrefix = ""
    @State var videoSuffix = ""
    @State var applied: AppliedStyle?                  // the recipe in use, with what it set
    @State var styleNote: String?                      // "That recipe's model isn't on your computer"
    @State var quickTick = 0                          // bumped when a quick pick is tapped (for its haptic)
    @State var createSignature = ""                    // the form as it was when the run started
    @State var reviseSignature = ""
    @State var videoSignature = ""
    @State var startTick = 0
    @State var stopTick = 0
    @State var panelHeight: CGFloat = 260              // the panel's height right now, whatever it shows (the picture is fitted above it)
    @GestureState var panelDrag: CGFloat = 0
    @State var dockHeight: CGFloat = 200
    @State var pageHeight: CGFloat = 220               // the open drawer page's own height (the drawer is no taller than it needs)
    @State var noteDismissed: String?
    @State var resultsAtStart = 0                      // how many results there were when the last Create began

    struct LoraPick: Identifiable { let id = UUID(); var name: String; var strength: Double }

    var recipe: CreateRecipe? { recipes.first { $0.id == recipeID } }

    var body: some View {
        NavigationStack {
            screenEffects
        }
    }

    /// The screen itself, with the handlers that keep the form in step with the store and the pickers.
    var screenBase: some View {
            ZStack {
                Fx.canvas.ignoresSafeArea()
                if !desktop.isPaired {
                    unpairedCard
                } else {
                    makeStage
                }
            }
            .tabBarClearance()
            .toolbar(.hidden, for: .navigationBar)
            .toolbar {
                ToolbarItemGroup(placement: .keyboard) {
                    Spacer()
                    Button("Done") { Self.hideKeyboard() }
                }
            }
            .onChange(of: store.revisedResultID) { _, id in if let id { workingID = id } }
            .onChange(of: store.results.count) { old, new in
                if new > old, store.reviseStatus == nil { workingID = nil }       // a new picture: work on it
            }
            .onChange(of: createRunning) { was, now in
                // A Create that made pictures ends: the dock moves on to changing the newest one. Not while the
                // settings are open: then the form is being tuned, and the next tap is Create again.
                if was, !now, mode == "picture", store.results.count > resultsAtStart, !panelOpen { target = .change }
            }
            .onChange(of: working?.id) { _, _ in
                if let w = working { changeMode = CreateStore.defaultChangeMode(for: w.recipeID) }
                if working == nil || working?.videoURL != nil { target = .new }
            }
            .onChange(of: morePick) { _, item in
                Task {
                    if let data = try? await item?.loadTransferable(type: Data.self), let ui = UIImage(data: data),
                       morePhotos.count < 2 {
                        morePhotos.append(ImageKit.upright(ui))
                    }
                    morePick = nil
                }
            }
            .onChange(of: photoPick) { _, item in
                Task {
                    if let data = try? await item?.loadTransferable(type: Data.self), let ui = UIImage(data: data) {
                        let up = ImageKit.upright(ui)
                        photo = up
                        openSection(.photo)                       // straight to what to do with it: edit, redraw or reference
                        LibraryStore.shared.add(picture: up, meta: Made(source: .added, action: "added")
                            .meta(kind: .picture, conversation: nil))
                    }
                    photoPick = nil
                }
            }
            .onChange(of: videoPhotoPick) { _, item in
                Task {
                    if let data = try? await item?.loadTransferable(type: Data.self), let ui = UIImage(data: data) {
                        let up = ImageKit.upright(ui)
                        videoPhoto = up
                        videoPhotoID = LibraryStore.shared.add(picture: up, meta: Made(source: .added, action: "added")
                            .meta(kind: .picture, conversation: nil))
                    }
                    videoPhotoPick = nil
                }
            }
            .onChange(of: makeExplicit) { _, on in explicitSwitchChanged(on) }
    }

    /// The second half of the screen's handlers (loading, sheets, clear everything, haptics), kept apart so no single
    /// modifier chain gets long enough to slow the compiler down.
    var screenEffects: some View {
        screenBase
            .task { await load(); applyMakeAgain() }
            .onAppear {
                if editor == "flux" { editor = "auto" }              // that editor is gone
                applyMakeAgain(); Desktop.shared.warmUpMake()
                Task { await recipeStore.refresh() }                 // every time Make opens: edits on the computer show at once
            }
            .onChange(of: desktop.isPaired) { _, paired in
                if paired { Task { await load(); applyMakeAgain(); await recipeStore.refresh() } }
            }
            .onChange(of: startTick) { _, _ in noteDismissed = nil }
            .onChange(of: rawNote) { _, n in if n == nil { noteDismissed = nil } }
            .sheet(isPresented: $showQueue) { NavigationStack { QueueScreen(inSheet: true) } }
            .sheet(isPresented: $showModels) { MakeModelSheet(recipes: recipes, chosen: modelChoice) }
            .sheet(isPresented: $showHelpers) {
                if let r = recipe { PromptHelpers(groups: r.toggles, chosen: $chosen) }
            }
            .onChange(of: router.clearedCount) { _, _ in
                workingID = nil; changeText = ""
                running?.cancel(); running = nil; runToken = nil
                photo = nil; morePhotos = []; preview = nil; status = nil; viewing = nil
                videoPhoto = nil; videoPhotoID = nil
                texts["prompt"] = ""; videoPrompt = ""
                stylePrefix = ""; styleSuffix = ""; videoPrefix = ""; videoSuffix = ""
                applied = nil; target = .new
                recipeStore.clear()
            }
            .fullScreenCover(item: $viewing) { r in
                MakeMediaViewer(startID: r.id, chat: chat, router: router, onTweak: { res in fill(from: res) })
            }
            .onChange(of: router.makeAgain) { _, meta in
                if meta != nil { applyMakeAgain() }
            }
            .sensoryFeedback(.impact, trigger: startTick)
            .sensoryFeedback(.impact(weight: .light), trigger: stopTick)
            .sensoryFeedback(.success, trigger: store.results.count)
            .sensoryFeedback(.selection, trigger: applied?.id)
            .sensoryFeedback(.selection, trigger: quickTick)
            .sensoryFeedback(.impact(weight: .light), trigger: makeExplicit)
    }

    /// "Make another" from the Library or chat: applied once the models have loaded, so the model and seed restore.
    func applyMakeAgain() {
        guard let meta = router.makeAgain, !loading else { return }
        router.makeAgain = nil
        fill(from: meta)
    }

    /// Back into the form exactly as that picture was made, same seed: change words, redo.
    func fill(from res: CreateResult) {
        if res.videoURL != nil {
            mode = "video"
            target = .new
            videoPrompt = res.texts["prompt"] ?? ""
            videoPrefix = ""; videoSuffix = ""; applied = nil
            if let e = res.explicit { rules.explicitVideo = e }
            videoPhoto = nil; videoPhotoID = nil
            return
        }
        mode = "picture"
        target = .new
        stylePrefix = ""; styleSuffix = ""; applied = nil
        if recipes.contains(where: { $0.id == res.recipeID }) { recipeID = res.recipeID }
        texts = res.texts
        numbers = res.numbers
        menus = res.menus
        shape = res.shape
        chosen = Set(res.toggles)
        fixedSeed = res.seed
        photo = nil
        morePhotos = []
    }

    /// The same, from a Library item (a Make picture keeps its whole form; a chat picture keeps its words and seed).
    func fill(from meta: LibraryMeta) {
        if meta.kind == .video {
            mode = "video"
            target = .new
            videoPrompt = meta.words ?? ""
            videoPrefix = ""; videoSuffix = ""; applied = nil
            if let e = meta.explicitVideo { rules.explicitVideo = e }
            videoPhoto = nil; videoPhotoID = nil
            return
        }
        mode = "picture"
        target = .new
        stylePrefix = ""; styleSuffix = ""; applied = nil
        let id = meta.recipeID.flatMap { rid in recipes.contains(where: { $0.id == rid }) ? rid : nil }
            ?? recipes.first(where: { $0.model != nil && $0.model == meta.model })?.id
        if let id { recipeID = id }
        texts = meta.texts ?? ["prompt": meta.words ?? ""]
        if texts["prompt"] == nil { texts["prompt"] = meta.words ?? "" }
        numbers = meta.numbers ?? [:]
        menus = meta.menus ?? [:]
        shape = meta.shape ?? "square"
        chosen = Set(meta.toggles ?? [])
        loras = (meta.loras ?? []).map { LoraPick(name: $0.name, strength: $0.strength) }
        fixedSeed = meta.seed
        photo = nil
        morePhotos = []
    }

    static func hideKeyboard() {
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
    }

    /// The picture being worked on, right under the form (11-17: changes were buried in the full-screen viewer and
    /// "send to chat" switched engines). Flip through this session's pictures with the arrows, say what to change,
    /// watch it form live; each version lands in the results. Nothing leaves Make.
    var working: CreateResult? {
        if let workingID, let r = store.results.first(where: { $0.id == workingID }) { return r }
        return store.results.last
    }

    func applyChange(_ w: CreateResult) {
        let words = changeText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !words.isEmpty, w.videoURL == nil else { return }
        Self.hideKeyboard()
        store.revise(baseID: w.id, change: words, mode: changeMode, mask: nil, strength: nil, editor: editor,
                     explicit: changeMode == "edit" && editor == "qwen21" ? addonValue : nil)
        changeText = ""
    }

    @ViewBuilder
    func advancedControl(_ c: CreateControl) -> some View {
        switch c.kind {
        case "text":
            VStack(alignment: .leading) {
                Text(c.label).font(.footnote).foregroundStyle(.secondary)
                TextEditor(text: Binding(get: { texts[c.key] ?? "" }, set: { texts[c.key] = $0 })).frame(minHeight: 70)
                if let h = c.help { Text(h).font(.caption).foregroundStyle(.secondary) }
            }
        case "seed":
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("Seed")
                    Spacer()
                    if let s = fixedSeed {
                        Text(String(s)).monospacedDigit().foregroundStyle(.secondary)
                        Button("Random") { fixedSeed = nil }.buttonStyle(.borderless)
                    } else {
                        Text("Random").foregroundStyle(.secondary)
                    }
                }
                if let h = c.help { Text(h).font(.caption).foregroundStyle(.secondary) }
            }
        default:
            numberOrMenu(c)
        }
    }

    @ViewBuilder
    func numberOrMenu(_ c: CreateControl) -> some View {
        if c.kind == "menu" {
            VStack(alignment: .leading, spacing: 2) {
                Picker(c.label, selection: Binding(get: { menus[c.key] ?? c.defaultText ?? c.options.first ?? "" },
                                                   set: { menus[c.key] = $0 })) {
                    ForEach(c.options, id: \.self) { Text($0).tag($0) }
                }
                .pickerStyle(.menu)
                if let h = c.help { Text(h).font(.caption).foregroundStyle(.secondary) }
            }
        } else {
            let value = Binding(get: { numbers[c.key] ?? c.defaultNumber ?? c.min }, set: { numbers[c.key] = $0 })
            VStack(alignment: .leading, spacing: 2) {
                HStack {
                    Text(c.label)
                    Spacer()
                    Text(c.kind == "int" ? String(Int(value.wrappedValue)) : String(format: "%.1f", value.wrappedValue))
                        .monospacedDigit().foregroundStyle(.secondary)
                }
                if c.kind == "int", c.max - c.min <= 10 {
                    Stepper(c.label, value: value, in: c.min...c.max, step: c.step).labelsHidden()
                } else {
                    Slider(value: value, in: c.min...c.max, step: c.step)
                    if c.left != nil || c.right != nil {
                        HStack {
                            Text("← \(c.left ?? "")")
                            Spacer()
                            Text("\(c.right ?? "") →")
                        }
                        .font(.caption2).foregroundStyle(.secondary)
                    }
                }
                if let h = c.help { Text(h).font(.caption).foregroundStyle(.secondary) }
            }
        }
    }

    /// With a photo, any model can work on it (11-17: "if I upload a pic, let me choose the model"): an editor keeps
    /// the rest of the photo; a picture model redraws it from the photo ("Start from it", with the strength slider).
    var photoModelChoice: Binding<String> {
        Binding(get: { photoMode == "start" ? "recipe:" + recipeID : editor },
                set: { v in
                    if v.hasPrefix("recipe:") {
                        recipeID = String(v.dropFirst("recipe:".count))
                        photoMode = "start"
                    } else {
                        editor = v
                        if photoMode == "start" { photoMode = "edit" }
                    }
                })
    }

    var photoModelPicker: some View {
        VStack(alignment: .leading, spacing: 2) {
            Picker("Model for this photo", selection: photoModelChoice) {
                Section("Keep the rest of the photo") {
                    Text("Auto").tag("auto")
                    Text("Qwen Edit").tag("qwen")
                    Text("Qwen Edit Explicit").tag("qwen-explicit")
                    if hasQwen21 { Text("Qwen-Image 2.1").tag("qwen21") }
                }
                Section("Redraw it with") {
                    ForEach(recipes) { r in Text(r.name).tag("recipe:" + r.id) }
                }
            }
            .pickerStyle(.menu)
            Text(photoMode == "start"
                 ? "Redraws your photo with \(recipe?.name ?? "this model"): its look and strengths, keeping the layout. "
                   + "Set how much changes below. Poses rarely change this way; use an editor for that."
                 : editorHelp)
                .font(.caption).foregroundStyle(.secondary)
            if photoMode != "start" { explicitAddonPicker }
        }
    }

    /// Whether the computer has Qwen-Image 2.1 (the editor choice and its Explicit add-on only show then).
    var hasQwen21: Bool { recipes.contains { $0.id == "qwen21" } }

    /// Which Enhance switch the prompt box shows: a photo being edited or used as a reference follows the changes
    /// switch (off unless turned on); everything else follows the new-pictures switch (on unless turned off).
    var promptEnhance: Binding<Bool> {
        photo != nil && photoMode != "start" ? $enhanceEdits : $enhanceNew
    }

    /// The choices the computer offers for the Explicit add-on (just Off when its file isn't on the computer).
    var addonOptions: [String] {
        let o = recipes.first { $0.id == "qwen21" }?.controls.first { $0.key == "explicit" }?.options ?? []
        return o.isEmpty ? ["Auto", "On", "Off"] : o
    }

    /// The saved switch, kept to what the computer offers right now.
    var addonValue: String { addonOptions.contains(explicitAddon) ? explicitAddon : (addonOptions.first ?? "Off") }

    /// The Explicit add-on for a photo edit with Qwen-Image 2.1. (Making a picture with 2.1 has its own switch among the
    /// settings; this one is for the photo editor.)
    @ViewBuilder
    var explicitAddonPicker: some View {
        if editor == "qwen21" {
            VStack(alignment: .leading, spacing: 2) {
                if addonOptions.count > 1 {
                    Picker("Explicit add-on", selection: Binding(get: { addonValue }, set: { explicitAddon = $0 })) {
                        ForEach(addonOptions, id: \.self) { Text($0).tag($0) }
                    }
                    .pickerStyle(.menu)
                    Text("On loads the explicit add-on for nudity and sex; Off never does. Auto uses it only when the picture you change was made with it on. Your words are never changed.")
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    Text("The explicit add-on isn't on your computer yet, so Qwen-Image 2.1 edits without it.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }

    /// Which editor changes a photo, with what each is for.
    var editorPicker: some View {
        VStack(alignment: .leading, spacing: 2) {
            Picker("Editor", selection: $editor) {
                Text("Auto").tag("auto")
                Text("Qwen Edit").tag("qwen")
                Text("Qwen Edit Explicit").tag("qwen-explicit")
                if hasQwen21 { Text("Qwen-Image 2.1").tag("qwen21") }
            }
            .pickerStyle(.menu)
            Text(editorHelp).font(.caption).foregroundStyle(.secondary)
            explicitAddonPicker
        }
    }

    var editorHelp: String {
        switch editor {
        case "qwen": "Best at following you: poses, clothes, backgrounds, and combining up to 3 photos. About 20-40 s."
        case "qwen-explicit": "The same editor trained on explicit content: nudity and adult changes come out as asked."
        case "qwen21": "The new Qwen-Image 2.1: the same model as its pictures, so it starts at once after one. Use the Explicit add-on switch below for nudity."
        default: "Picks for you: Qwen-Image 2.1 for pictures made with it, Qwen Edit Explicit when your words are explicit or you're on an explicit model, otherwise Qwen Edit."
        }
    }

    func numbered(_ ui: UIImage, _ n: Int, remove: (() -> Void)?) -> some View {
        Image(uiImage: ui).resizable().scaledToFill().frame(width: 64, height: 64).clipped()
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .overlay(alignment: .topLeading) {
                Text("\(n)").font(.caption.bold()).padding(4).background(.ultraThinMaterial, in: Circle()).padding(3)
            }
            .overlay(alignment: .topTrailing) {
                if let remove {
                    Button(action: remove) { Image(systemName: "xmark.circle.fill").foregroundStyle(.white, .black.opacity(0.6)) }
                        .buttonStyle(.borderless).padding(2)
                        .accessibilityLabel("Remove photo \(n)")
                }
            }
            .accessibilityLabel("Photo \(n)")
    }

    var photoHelp: String {
        switch photoMode {
        case "start": "Redraws your photo with the recipe above. Low: barely touched. High: only the rough layout stays."
        case "reference": "A new picture in the shape below that borrows the person, outfit, object or style from your photos."
        default: "Say what to change: a pose (\"make her sit on the bed\"), clothes, the background, the light. Everything you don't mention stays."
        }
    }

    func shapeIcon(_ s: String) -> String {
        switch s {
        case "portrait": "rectangle.portrait"
        case "landscape": "rectangle"
        case "wallpaper": "iphone"
        default: "square"
        }
    }

    func load() async {
        guard Desktop.shared.isPaired else { loading = false; return }
        loading = true
        failed = nil
        do {
            let r = try await Desktop.shared.createRecipes()
            recipes = r.recipes
            loraFiles = r.loras
            loraLibrary = r.library
            videoLoraFamily = r.videoFamily
            if recipe == nil { recipeID = recipes.first?.id ?? "" }
            if editor == "qwen21", !hasQwen21 { editor = "auto" }      // this computer doesn't have it
        } catch {
            failed = "Couldn't reach your computer. Check that Fixer Desktop is on."
        }
        loading = false
    }

    func generate(_ r: CreateRecipe) {
        var values: [String: Any] = ["shape": shape]
        for c in r.controls {
            switch c.kind {
            case "text": values[c.key] = texts[c.key] ?? ""
            case "menu": values[c.key] = menus[c.key] ?? c.defaultText ?? ""
            case "int": values[c.key] = Int(numbers[c.key] ?? c.defaultNumber ?? c.min)
            case "float": values[c.key] = numbers[c.key] ?? c.defaultNumber ?? c.min
            default: break
            }
        }
        if let s = fixedSeed { values["seed"] = s }
        // The prompt the model gets: the recipe's opening words, the owner's words, its closing words.
        let assembled = assembledPrompt
        values["prompt"] = assembled
        if let applied, applied.kind == "picture" { values["style"] = applied.id }
        var explicitOverride: String?              // the photo editor's add-on switch, when it replaces the recipe's own
        // "Enhance my prompt": a photo being edited or used as a reference follows the changes switch.
        let enhanceOn = (photo != nil && photoMode != "start") ? enhanceEdits : enhanceNew
        if enhanceOn { values["enhance"] = true }
        if let photo, let cg = ImageKit.cgImage(photo), let jpeg = Desktop.jpeg(cg, longest: 1536, quality: 0.92) {
            values["image"] = jpeg
            values["image_mode"] = photoMode
            values["strength"] = strength
            values["editor"] = editor
            if editor == "qwen21", photoMode != "start" {
                explicitOverride = addonValue
                values["explicit"] = addonValue
            }
            if photoMode != "start" {
                values["images"] = morePhotos.compactMap { ui in
                    ImageKit.cgImage(ui).flatMap { Desktop.jpeg($0, longest: 1536, quality: 0.92) }
                }
            }
        }
        let picked = activeLoras                    // only the add-ons that fit what is being made
        values["loras"] = picked.map { p -> [String: Any] in ["name": p.name, "strength": p.strength] }
        let known = Set(r.toggles.flatMap(\.items).map(\.id))
        values["toggles"] = chosen.filter { known.contains($0) }.sorted()
        preview = nil
        progress = 0
        status = "Starting…"
        createSignature = formSignature
        resultsAtStart = store.results.count
        startTick += 1
        store.blockedNote = nil
        let hasPhoto = photo != nil
        let epoch = store.epoch
        // The form as it is now, so the Library and "Make again" get exactly what made the picture.
        var textsNow = texts
        textsNow["prompt"] = assembled
        let formTexts = textsNow
        let formNumbers = numbers, formShape = shape
        var menusNow = menus
        if let explicitOverride { menusNow["explicit"] = explicitOverride }   // so a later change copies the same switch
        let formMenus = menusNow
        let formToggles = Array(chosen).sorted()
        let formLoras = picked.map { LoraSetting(name: $0.name, strength: $0.strength) }
        let formMode = hasPhoto ? photoMode : nil
        let formStrength = hasPhoto && photoMode == "start" ? strength : nil
        running?.cancel()                                  // 11-17: a new Create replaces the one in progress
        let mine = UUID()
        runToken = mine
        running = Task {
            do {
                try await Desktop.shared.create(recipe: r.id, values: values, slot: "make:create") { event in
                    Task { @MainActor in
                        switch event {
                        case .status(let s): status = s
                        case .progress(let p): progress = p
                        case .preview(let ui): preview = ui
                        case .image(let ui, let seed, let enhanced):
                            preview = nil
                            let f = hasPhoto ? Rules.shared.editFilter : Rules.shared.newFilter
                            if let cg = ui.cgImage, await ContentGate.blocks(cg, f) {
                                if epoch == store.epoch { store.noteBlocked() }            // not shown, not kept
                                return
                            }
                            guard epoch == store.epoch else { return }          // Clear everything ran meanwhile
                            var meta = Made(source: .make, action: hasPhoto ? "edit" : "make",
                                            words: formTexts["prompt"], model: r.model ?? r.id, recipeID: r.id,
                                            choice: r.id, seed: seed).meta(kind: .picture, conversation: nil)
                            meta.texts = formTexts
                            meta.numbers = formNumbers
                            meta.menus = formMenus
                            meta.shape = formShape
                            meta.toggles = formToggles
                            meta.loras = formLoras
                            meta.imageMode = formMode
                            meta.strength = formStrength
                            meta.enhanced = enhanced
                            let libraryID = LibraryStore.shared.add(picture: ui, meta: meta)
                            store.results.append(CreateResult(image: ui, seed: seed, recipeID: r.id, texts: formTexts,
                                                              numbers: formNumbers, menus: formMenus, shape: formShape,
                                                              toggles: formToggles, libraryID: libraryID, enhanced: enhanced))
                        }
                    }
                }
            } catch is CancellationError {
            } catch PCError.stopped(_) {
            } catch {
                if runToken == mine {
                    status = error.localizedDescription
                    try? await Task.sleep(nanoseconds: 2_500_000_000)
                }
            }
            if runToken == mine { running = nil }
        }
    }
}

/// "Enhance" beside a prompt box: filled when on, one tap toggles. The computer's chat model adds detail to the words
/// before drawing and never softens or drops anything asked for.
struct EnhanceChip: View {
    @Binding var isOn: Bool

    var body: some View {
        Button { isOn.toggle() } label: {
            Label("Enhance", systemImage: "sparkles")
                .font(.footnote.weight(.medium))
                .foregroundStyle(isOn ? Color.white : Color.accentColor)
                .padding(.horizontal, 10).padding(.vertical, 6)
                .background(Capsule().fill(isOn ? Color.accentColor : Color.accentColor.opacity(0.14)))
                .frame(minHeight: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.borderless)
        .accessibilityLabel("Enhance my prompt")
        .accessibilityValue(isOn ? "On" : "Off")
        .accessibilityHint("Your computer adds detail to your words before drawing. Nothing you asked for is removed.")
    }
}

/// The models to choose from in Make, as cards: a name and one line about each. (No Auto here: choices are explicit.)
struct MakeModelSheet: View {
    let recipes: [CreateRecipe]
    @Binding var chosen: String
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                ForEach(recipes) { r in
                    Button { chosen = r.id; dismiss() } label: {
                        HStack(alignment: .top) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(r.name).foregroundStyle(.primary)
                                if let note = r.note { Text(note).font(.footnote).foregroundStyle(.secondary) }
                            }
                            Spacer()
                            if chosen == r.id { Image(systemName: "checkmark").foregroundStyle(Color.accentColor) }
                        }
                        .frame(minHeight: 44)
                        .contentShape(Rectangle())
                    }
                    .accessibilityAddTraits(chosen == r.id ? .isSelected : [])
                }
            }
            .safeAreaInset(edge: .bottom) {
                Text("Picking a model sets it for the pictures you create here, with its recommended settings. "
                     + "Changes under a picture use that picture's model to redraw, or a photo editor to keep the rest.")
                    .font(.footnote).foregroundStyle(.secondary)
                    .padding().frame(maxWidth: .infinity, alignment: .leading).background(.bar)
            }
            .navigationTitle("Model")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() } } }
        }
        .presentationDetents([.large])
    }
}

/// The prompt helpers: groups of tap-on/off phrasings (from the computer, worded for the chosen model).
struct PromptHelpers: View {
    let groups: [PromptToggleGroup]
    @Binding var chosen: Set<String>
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                ForEach(groups) { g in
                    Section(g.name) {
                        ForEach(g.items) { t in
                            Button {
                                if chosen.contains(t.id) { chosen.remove(t.id) } else { chosen.insert(t.id) }
                            } label: {
                                HStack {
                                    Text(t.label).foregroundStyle(.primary)
                                    Spacer()
                                    if chosen.contains(t.id) {
                                        Image(systemName: "checkmark").foregroundStyle(Color.accentColor)
                                    }
                                }
                            }
                        }
                    }
                }
            }
            .navigationTitle("Style tips")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) { Button("Clear") { chosen.removeAll() }.disabled(chosen.isEmpty) }
                ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() } }
            }
        }
        .presentationDetents([.medium, .large])
    }
}

// MARK: - The viewer for this session's results

/// Make's side of the shared viewer: everything made in Make this session, newest first (the same order as the grid).
/// Changes run here and land in the same list, and the viewer moves to the new one.
struct MakeMediaViewer: View {
    let chat: Chat
    let router: AppRouter
    let onTweak: (CreateResult) -> Void
    @ObservedObject private var store = CreateStore.shared
    @Environment(\.dismiss) private var dismiss
    @State private var current: UUID
    @State private var note: String?

    init(startID: UUID, chat: Chat, router: AppRouter, onTweak: @escaping (CreateResult) -> Void) {
        self.chat = chat
        self.router = router
        self.onTweak = onTweak
        _current = State(initialValue: startID)
    }

    private var items: [MediaItem] {
        store.results.reversed().map {
            if let url = $0.videoURL {
                return MediaItem(id: $0.id, kind: .video, source: .chatVideo(url), libraryID: $0.libraryID, note: $0.note)
            }
            return MediaItem(id: $0.id, kind: .picture, source: .memory($0.image), libraryID: $0.libraryID,
                             recipeID: $0.recipeID, note: $0.note, enhanced: $0.enhanced)
        }
    }

    private var actions: MediaActions {
        var a = MediaActions()
        a.change = { item, words, mask, how in
            store.revise(baseID: item.id, change: words, mode: how.mode, mask: mask, strength: how.strength)
        }
        a.cancel = { store.cancelRevise() }
        a.animate = { item, words in
            guard case .memory(let ui) = item.source else { return }
            store.startVideo(prompt: words, start: ui, explicit: Rules.shared.explicitVideo,
                             parent: item.libraryID, action: "animate")
        }
        // No "send to chat" (11-17): work made in Make stays in Make; the chat would switch models to pick it up.
        a.makeAnother = { item in
            if let r = store.results.first(where: { $0.id == item.id }) { onTweak(r) }
            dismiss()
        }
        return a
    }

    var body: some View {
        MediaViewer(items: items, current: $current, actions: actions, busy: store.reviseStatus ?? store.videoStatus,
                    note: store.reviseNote ?? store.videoNote ?? note)
            .onChange(of: store.revisedResultID) { _, id in if let id { current = id } }
            .onChange(of: current) { _, _ in note = nil; store.reviseNote = nil; store.videoNote = nil }
    }
}
