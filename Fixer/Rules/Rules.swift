import SwiftUI

/// Everything the owner decides about how Fixer behaves. Stored on the phone only (UserDefaults).
/// The owner's standing instructions are the assistant's rules; Fixer adds only the technical
/// action format it needs to work on photos, shown in full on the Settings screen.
@MainActor
final class Rules: ObservableObject {
    static let shared = Rules()

    struct BrainChoice: Identifiable, Hashable {
        let id: String        // Hugging Face repo id (MLX format)
        let label: String
        let note: String
    }

    /// Presets; the owner can also paste any MLX vision model id.
    static let presets: [BrainChoice] = [
        BrainChoice(id: "EZCon/Huihui-Qwen3-VL-4B-Instruct-abliterated-4bit-mlx", label: "Qwen3-VL 4B, unrestricted",
                    note: "Default. Community build with refusals removed. Sees photos, finds things, reasons. 3.1 GB."),
        BrainChoice(id: "mlx-community/Qwen3-VL-4B-Instruct-4bit", label: "Qwen3-VL 4B, stock",
                    note: "The original model. 3.1 GB."),
        BrainChoice(id: "mlx-community/Qwen3-VL-2B-Instruct-4bit", label: "Qwen3-VL 2B, fast",
                    note: "Quicker and lighter, a little less clever. 1.8 GB."),
    ]

    /// Highest the nudity threshold goes when the generator edits an existing photo (never fully off).
    static let editExplicitCap = 1.00

    private static let d = UserDefaults.standard
    private static func double(_ k: String, _ def: Double) -> Double { d.object(forKey: k) as? Double ?? def }

    /// Starting personality, shown and editable in Settings > Custom instructions (never hidden). Used until the
    /// owner saves his own text; "Reset to default" brings it back.
    static var defaultPersonality: String { RemoteConfig.string("default_personality") ?? builtinPersonality }
    static let builtinPersonality = """
    You're Fixer, my personal assistant: whatever I throw at you, you handle it. Easygoing, sharp, a little cocky \
    in the good way, like the friend who just gets things done. Talk like a person: short, direct, some swagger, a \
    dry joke when it fits, never corporate and never stiff. Answer what I asked first, at the length it deserves: \
    a line for simple things, more when it matters. Use lists or steps only when they help. Don't pad, don't \
    repeat the question back, don't close with a menu of things you could do, and don't talk about yourself, your \
    tools or how you work unless I ask. Give real opinions when asked, say when you're unsure, and match my mood.
    """
    @Published var instructions: String = Rules.d.string(forKey: "rules.instructions") ?? Rules.defaultPersonality {
        didSet { if !refreshing { Self.d.set(instructions, forKey: "rules.instructions") } } }
    private var refreshing = false
    /// A new default from the computer reaches an owner who never wrote his own instructions.
    func refreshDefaultPersonality() {
        guard Self.d.string(forKey: "rules.instructions") == nil, instructions != Self.defaultPersonality else { return }
        refreshing = true
        instructions = Self.defaultPersonality
        refreshing = false
    }
    /// Send the owner's own words to the picture model instead of the chat model's prompt.
    @Published var useMyWords: Bool = Rules.d.object(forKey: "rules.useMyWords") as? Bool ?? false {
        didSet { Self.d.set(useMyWords, forKey: "rules.useMyWords") } }
    @Published var brainID: String = Rules.d.string(forKey: "rules.brainID") ?? Rules.presets[0].id {
        didSet { Self.d.set(brainID, forKey: "rules.brainID") } }
    /// Any Core ML Stable Diffusion 1.x/2.x repo on Hugging Face.
    @Published var generatorID: String = Rules.d.string(forKey: "rules.generatorID") ?? ModelStore.defaultGenerator {
        didSet {
            guard generatorID != oldValue else { return }
            Self.d.set(generatorID, forKey: "rules.generatorID")
            ModelStore.shared.delete(.generator)      // the new one downloads on next use
            Task { await Generator.shared.unload() }  // drop the old one from memory too
        } }
    @Published var temperature: Double = Rules.double("rules.temperature", 0.7) {   // Qwen chat setting
        didSet { Self.d.set(temperature, forKey: "rules.temperature") } }
    @Published var maxTokens: Int = Rules.d.object(forKey: "rules.maxTokens") as? Int ?? 1600 {
        didSet { Self.d.set(maxTokens, forKey: "rules.maxTokens") } }
    @Published var editStrength: Double = Rules.double("rules.editStrength", 0.6) {
        didSet { Self.d.set(editStrength, forKey: "rules.editStrength") } }
    @Published var steps: Int = Rules.d.object(forKey: "rules.steps") as? Int ?? 20 {
        didSet { Self.d.set(steps, forKey: "rules.steps") } }
    @Published var guidance: Double = Rules.double("rules.guidance", 7.5) {
        didSet { Self.d.set(guidance, forKey: "rules.guidance") } }
    /// Photo changes run without asking; only a video (2-3 minutes on the computer) asks first.
    @Published var askBeforeVideo: Bool = Rules.d.object(forKey: "rules.askBeforeVideo") as? Bool ?? true {
        didSet { Self.d.set(askBeforeVideo, forKey: "rules.askBeforeVideo") } }

    /// Think before answering (the computer's chat model reasons first; shown as "Thought for N s"). Off by default.
    @Published var thinkMode: Bool = Rules.d.bool(forKey: "rules.thinkMode") {
        didSet { Self.d.set(thinkMode, forKey: "rules.thinkMode") } }

    /// Which picture model the chat uses: "auto" (picked by the words) or a recipe id from the computer.
    /// (11-17: FLUX.2 klein and bigASP are gone from the computer; a saved choice of either becomes Auto.)
    @Published var chatModelChoice: String = {
        let s = Rules.d.string(forKey: "rules.chatModelChoice") ?? "auto"
        return s == "flux-klein" || s == "bigasp" ? "auto" : s
    }() {
        didSet {
            Self.d.set(chatModelChoice, forKey: "rules.chatModelChoice")
            Desktop.shared.warmUpMake(force: true)
        } }

    // MARK: My PC (Fixer Desktop)

    struct PCImageChoice: Identifiable, Hashable {
        let id: String, label: String, note: String, steps: Int, guidance: Double
    }
    static let defaultPCBrain = "llmfan46/Qwen3.6-35B-A3B-uncensored-heretic-Native-MTP-Preserved-GGUF"
    static let densePCBrain = "llmfan46/Qwen3.6-27B-uncensored-heretic-v2-GGUF"
    /// Chat models the computer no longer keeps: a saved one moves to the current default.
    static let retiredPCBrains: Set<String> = [
        Rules.densePCBrain,                 // 11-17: the 27B was removed from the computer; one brain, the 35B mixture-of-experts
        "huihui-ai/Huihui-Qwen3-VL-8B-Instruct-abliterated", "mradermacher/Huihui-Qwen3-VL-8B-Instruct-abliterated-GGUF",
        "unsloth/Qwen3-VL-8B-Instruct-GGUF", "Qwen/Qwen3-VL-8B-Instruct",
        "DavidAU/Qwen3.5-9B-The-Defiant-Fable-Uncensored-Heretic-NEO-IMATRIX-MAX-MTP-GGUF"]
    static func migratedPCBrain(_ stored: String?) -> String {
        // 11-17: the 35B mixture-of-experts brain became the default (the chat keeps answering while pictures are made).
        // A phone still on the old default moves once (the 27B is retired now, so it always lands on the default).
        if !d.bool(forKey: "rules.pcBrainMoE.v1") {
            d.set(true, forKey: "rules.pcBrainMoE.v1")
            if stored == nil || stored == densePCBrain {
                d.set(defaultPCBrain, forKey: "rules.pcBrainID")     // didSet doesn't run in the initializer
                return defaultPCBrain
            }
        }
        guard let stored, !retiredPCBrains.contains(stored) else { return defaultPCBrain }
        return stored
    }
    static let pcImagePresets: [PCImageChoice] = [
        PCImageChoice(id: "Tongyi-MAI/Z-Image-Turbo", label: "Z-Image Turbo",
                      note: "Very photographic. Edits by repainting with Edit strength.", steps: 9, guidance: 0.0),
    ]
    /// Picture models the computer no longer keeps (11-17: FLUX.2 klein and bigASP were removed): a saved one moves
    /// to the first preset.
    static func isRemovedPCImage(_ id: String) -> Bool {
        let l = id.lowercased()
        return l.contains("flux.2-klein") || l.contains("flux2-klein") || l.contains("bigasp")
    }
    /// The saved picture model, moved off a removed one. The saved steps and guidance move with it (they belong to
    /// the model); this runs before those two are read, as stored properties start in the order they are written.
    private static func migratedPCImage(_ stored: String?) -> String {
        let first = pcImagePresets[0]
        if stored == nil, d.object(forKey: "rules.pcSteps") == nil {
            d.set(first.steps, forKey: "rules.pcSteps")
            d.set(first.guidance, forKey: "rules.pcGuidance")
        }
        guard let stored, !isRemovedPCImage(stored) else {
            if stored != nil {
                d.set(first.steps, forKey: "rules.pcSteps")
                d.set(first.guidance, forKey: "rules.pcGuidance")
            }
            return first.id
        }
        return stored
    }

    @Published var usePC: Bool = Rules.d.object(forKey: "rules.usePC") as? Bool ?? false {
        didSet { Self.d.set(usePC, forKey: "rules.usePC") } }
    /// The desktop runs chat models through llama.cpp (GGUF). The old transformers default maps to its GGUF build.
    @Published var pcBrainID: String = Rules.migratedPCBrain(Rules.d.string(forKey: "rules.pcBrainID")) {
        didSet { Self.d.set(pcBrainID, forKey: "rules.pcBrainID") } }
    @Published var pcImageID: String = Rules.migratedPCImage(Rules.d.string(forKey: "rules.pcImageID")) {
        didSet {
            Self.d.set(pcImageID, forKey: "rules.pcImageID")
            if let p = Self.pcImagePresets.first(where: { $0.id == pcImageID }) { pcSteps = p.steps; pcGuidance = p.guidance }
        } }
    /// Wan 2.2 image-to-video 14B in ComfyUI on the computer (11-17). Older choices (the 5B models, LTX) are gone
    /// from the computer, so any saved one moves to it.
    static let defaultPCVideo = "Comfy-Org/Wan_2.2_ComfyUI_Repackaged"
    @Published var pcVideoID: String = Rules.defaultPCVideo {
        didSet { Self.d.set(pcVideoID, forKey: "rules.pcVideoID") } }
    /// The video model's explicit add-on: "off" (never), "on" (every video), "auto" (only when the words are
    /// explicit, decided on the computer). the owner 11-17: a switch, so his wording is never second-guessed.
    @Published var explicitVideo: String = Rules.d.string(forKey: "rules.explicitVideo") ?? "off" {
        didSet { Self.d.set(explicitVideo, forKey: "rules.explicitVideo") } }
    @Published var pcSteps: Int = Rules.d.object(forKey: "rules.pcSteps") as? Int ?? 4 {
        didSet { Self.d.set(pcSteps, forKey: "rules.pcSteps") } }
    @Published var pcGuidance: Double = Rules.double("rules.pcGuidance", 1.0) {
        didSet { Self.d.set(pcGuidance, forKey: "rules.pcGuidance") } }
    @Published var pcSize: Int = Rules.d.object(forKey: "rules.pcSize") as? Int ?? 1024 {
        didSet { Self.d.set(pcSize, forKey: "rules.pcSize") } }

    /// SearXNG address the phone uses for web search when the PC isn't on (the PC uses its own setting).
    @Published var searchURL: String = Rules.d.string(forKey: "rules.searchURL") ?? "" {   // empty: the address the computer sends
        didSet { Self.d.set(searchURL, forKey: "rules.searchURL") } }
    @Published var searchResults: Int = Rules.d.object(forKey: "rules.searchResults") as? Int ?? 6 {
        didSet { Self.d.set(searchResults, forKey: "rules.searchResults") } }

    // Output check thresholds (1.0 = never block). All off by default (the owner, 2026-11-16: filters stay off unless he
    // turns one on). Builds before that defaulted nudity to "block at 60%", so this update switches all four off
    // once; after that the sliders are entirely his.
    private static let filtersOffOnce: Void = {
        guard !d.bool(forKey: "rules.filtersOff.v1") else { return }
        for k in ["rules.editRevealing", "rules.editExplicit", "rules.newRevealing", "rules.newExplicit"] { d.set(1.0, forKey: k) }
        d.set(true, forKey: "rules.filtersOff.v1")
    }()
    private static func filter(_ k: String) -> Double { _ = filtersOffOnce; return double(k, 1.0) }

    @Published var editRevealing: Double = Rules.filter("rules.editRevealing") {
        didSet { Self.d.set(editRevealing, forKey: "rules.editRevealing") } }
    @Published var editExplicit: Double = min(Rules.filter("rules.editExplicit"), Rules.editExplicitCap) {
        didSet {
            if editExplicit > Self.editExplicitCap { editExplicit = Self.editExplicitCap }
            Self.d.set(editExplicit, forKey: "rules.editExplicit")
        } }
    @Published var newRevealing: Double = Rules.filter("rules.newRevealing") {
        didSet { Self.d.set(newRevealing, forKey: "rules.newRevealing") } }
    @Published var newExplicit: Double = Rules.filter("rules.newExplicit") {
        didSet { Self.d.set(newExplicit, forKey: "rules.newExplicit") } }

    var editFilter: FilterSetting { FilterSetting(revealing: editRevealing, explicit: min(editExplicit, Self.editExplicitCap)) }
    var newFilter: FilterSetting { FilterSetting(revealing: newRevealing, explicit: newExplicit) }

    private init() {}
}
