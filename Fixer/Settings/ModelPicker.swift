import SwiftUI

/// The four model choices. Settings shows only "Chat model" and "Image model"; which pair it edits depends on
/// whether Fixer Desktop is in use.
enum ModelSlot: String, CaseIterable, Identifiable {
    case phoneChat, phoneImage, desktopChat, desktopImage, desktopVideo
    var id: String { rawValue }

    /// Runs on Fixer Desktop (its models live on the computer).
    var onPC: Bool { self == .desktopChat || self == .desktopImage || self == .desktopVideo }

    var title: String {
        switch self {
        case .phoneChat, .desktopChat: "Chat model"
        case .phoneImage, .desktopImage: "Image model"
        case .desktopVideo: "Video model"
        }
    }

    /// Hugging Face list query for models this slot can run.
    var query: [URLQueryItem] {
        switch self {
        case .phoneChat: [.init(name: "filter", value: "mlx"), .init(name: "pipeline_tag", value: "image-text-to-text")]
        case .desktopChat: [.init(name: "filter", value: "gguf"), .init(name: "pipeline_tag", value: "image-text-to-text")]
        case .phoneImage: [.init(name: "filter", value: "coreml"), .init(name: "pipeline_tag", value: "text-to-image")]
        case .desktopImage: [.init(name: "library", value: "diffusers"), .init(name: "pipeline_tag", value: "text-to-image")]
        case .desktopVideo: [.init(name: "library", value: "diffusers"), .init(name: "pipeline_tag", value: "text-to-video")]
        }
    }

    /// Largest download that runs comfortably where this slot runs.
    var fitLimit: Double {
        switch self {
        case .phoneChat: 4.5e9
        case .phoneImage: 2.5e9
        case .desktopChat: 22e9
        case .desktopImage: 40e9
        case .desktopVideo: 60e9
        }
    }

    /// What this choice does, in plain words (11-17: "when I choose a model I just don't know").
    var explainer: String {
        switch self {
        case .phoneChat:
            "The brain that answers on this iPhone when your computer can't be reached. The number (2B, 4B) is its size: bigger is cleverer but slower and uses more of the phone's memory. \"Uncensored\" versions don't refuse or soften requests."
        case .desktopChat:
            "The brain on your computer: it talks with you, reads photos and decides what picture or video work to do. Bigger is cleverer but slower to start. \"Uncensored\" versions don't refuse or soften requests. Other models from the list download the first time you use them."
        case .phoneImage:
            "Draws pictures on this iPhone when your computer can't be reached."
        case .desktopImage:
            "The picture model the chat uses when you pick it instead of Auto. Make has its own model menu. Each one is good at different things; see the line under each."
        case .desktopVideo:
            "The model that makes videos on your computer."
        }
    }

    var builtIn: [ModelEntry] {
        switch self {
        case .phoneChat: [
            ModelEntry(id: "EZCon/Huihui-Qwen3-VL-4B-Instruct-abliterated-4bit-mlx", name: "Qwen3-VL 4B Uncensored", size: 3.1e9, note: "Answers on this iPhone when the computer isn't reachable. Sees photos. Won't refuse or water things down."),
            ModelEntry(id: "mlx-community/Qwen3-VL-4B-Instruct-4bit", name: "Qwen3-VL 4B", size: 3.1e9, note: "The same size, standard version: may refuse or soften adult requests."),
            ModelEntry(id: "mlx-community/Qwen3-VL-2B-Instruct-4bit", name: "Qwen3-VL 2B", size: 1.8e9, note: "Half the size: starts and answers faster, but noticeably less clever.")]
        case .desktopChat: [
            // 11-17: mixture of experts (3B active): experts in the computer's memory, the rest on the card.
            ModelEntry(id: "llmfan46/Qwen3.6-35B-A3B-uncensored-heretic-Native-MTP-Preserved-GGUF", name: "Qwen3.6 35B Uncensored", size: 19.8e9, note: "The computer's chat brain: quick, sees photos, plans picture and video jobs, and keeps answering while pictures are made (most of it runs from the computer's memory). Won't refuse or water things down.")]
        case .phoneImage: [
            ModelEntry(id: ModelStore.defaultGenerator, name: "Stable Diffusion 1.5", size: 1.0e9, note: "Draws pictures on this iPhone when the computer isn't reachable. Small and simple; the computer's models are far better.")]
        case .desktopImage: [
            // Sizes of the ComfyUI files the computer actually keeps (11-17).
            ModelEntry(id: "Kandoo/Juggernaut-XL-Ragnarok", name: "Juggernaut XL Ragnarok", size: 7.1e9, note: "Photo-real people, portraits and scenes, and fine with explicit pictures. Uses 'Leave out'. 30 steps."),
            ModelEntry(id: "Comfy-Org/Qwen-Image-2.1", name: "Qwen-Image 2.1 Turbo", size: 14.3e9, note: "New. Follows long, exact descriptions and lettering well; makes and edits with the same model. The explicit add-on is a switch."),
            ModelEntry(id: "lodestones/Chroma1-HD", name: "Chroma1 HD", size: 14.7e9, note: "Finest natural detail (skin, hair, fabric) and fine with explicit pictures. About three times slower.")]
        case .desktopVideo: [
            // Wan 2.2 image-to-video 14B in ComfyUI (11-17); the old 5B models were removed from the computer.
            ModelEntry(id: "Comfy-Org/Wan_2.2_ComfyUI_Repackaged", name: "Wan 2.2 14B", size: 38e9, note: "Turns words or a photo into a 5-second clip with natural motion. About 2-3 minutes per video.")]
        }
    }

    @MainActor var current: String {
        get {
            let r = Rules.shared
            switch self {
            case .phoneChat: return r.brainID
            case .phoneImage: return r.generatorID
            case .desktopChat: return r.pcBrainID
            case .desktopImage: return r.pcImageID
            case .desktopVideo: return r.pcVideoID
            }
        }
        nonmutating set {
            let r = Rules.shared
            switch self {
            case .phoneChat: r.brainID = newValue
            case .phoneImage: r.generatorID = newValue
            case .desktopChat: r.pcBrainID = newValue
            case .desktopImage: r.pcImageID = newValue
            case .desktopVideo: r.pcVideoID = newValue
            }
        }
    }

    @MainActor static func chat() -> ModelSlot { Desktop.shared.isOn ? .desktopChat : .phoneChat }
    @MainActor static func image() -> ModelSlot { Desktop.shared.isOn ? .desktopImage : .phoneImage }
}

struct ModelEntry: Identifiable, Hashable {
    let id: String
    var name: String
    var size: Double?          // bytes, estimated
    var downloads: Int?
    var likes: Int?
    var note: String? = nil    // what it's good at, in plain words (built-in choices)

    var author: String { id.split(separator: "/").first.map(String.init) ?? "" }

    /// "mlx-community/Qwen3-VL-4B-Instruct-4bit" -> "Qwen3 VL 4B Instruct"
    static func displayName(_ id: String) -> String {
        var n = id.split(separator: "/").last.map(String.init) ?? id
        for junk in ["-GGUF", "-gguf", "-4bit-mlx", "-8bit-mlx", "-mlx", "-4bit", "-8bit", "-bf16", "-fp16",
                     "-palettized", "-coreml", "_original_compiled", "_split_einsum_compiled", "-abliterated"] {
            n = n.replacingOccurrences(of: junk, with: "")
        }
        return n.replacingOccurrences(of: "-", with: " ").replacingOccurrences(of: "_", with: " ")
    }
}

/// Favourites, per slot, kept on the phone.
@MainActor
final class Favorites: ObservableObject {
    static let shared = Favorites()
    /// Built-ins that came after the first builds; everything else counts as already offered.
    private static let addedLater: Set<String> = ["lodestones/Chroma1-HD", "Kandoo/Juggernaut-XL-Ragnarok",
                                                  "Comfy-Org/Qwen-Image-2.1",
                                                  "llmfan46/Qwen3.6-35B-A3B-uncensored-heretic-Native-MTP-Preserved-GGUF"]
    /// 11-17: models the computer no longer keeps. A saved list drops them, so nothing offers a removed model.
    private static let removedIDs: Set<String> = ["black-forest-labs/FLUX.2-klein-9B", "GraydientPlatformAPI/bigasp2lustify",
                                                  "llmfan46/Qwen3.6-27B-uncensored-heretic-v2-GGUF",
                                                  "Tongyi-MAI/Z-Image-Turbo", "tewea/z_image_turbo_bf16_nsfw"]
    @Published private(set) var lists: [String: [String]] = [:]

    private init() {
        let d = UserDefaults.standard
        for slot in ModelSlot.allCases {
            var list = (d.stringArray(forKey: "fav.\(slot.rawValue)") ?? slot.builtIn.map(\.id))
                .filter { !Self.removedIDs.contains($0) }
            // Built-ins added in a later build (Chroma, 11-16) join a saved list once; one the owner removed after
            // that stays removed, because it's remembered as already offered.
            let offeredKey = "fav.offered.\(slot.rawValue)"
            let offered = Set(d.stringArray(forKey: offeredKey)
                              ?? slot.builtIn.map(\.id).filter { !Self.addedLater.contains($0) })
            for id in slot.builtIn.map(\.id) where !offered.contains(id) && !list.contains(id) { list.append(id) }
            d.set(Array(offered.union(slot.builtIn.map(\.id))), forKey: offeredKey)
            d.set(list, forKey: "fav.\(slot.rawValue)")
            lists[slot.rawValue] = list
        }
    }

    func ids(_ slot: ModelSlot) -> [String] { lists[slot.rawValue] ?? [] }
    func contains(_ id: String, _ slot: ModelSlot) -> Bool { ids(slot).contains(id) }

    func toggle(_ id: String, _ slot: ModelSlot) {
        var l = ids(slot)
        if let i = l.firstIndex(of: id) { l.remove(at: i) } else { l.append(id) }
        lists[slot.rawValue] = l
        UserDefaults.standard.set(l, forKey: "fav.\(slot.rawValue)")
    }

    func remove(at offsets: IndexSet, _ slot: ModelSlot) {
        var l = ids(slot)
        l.remove(atOffsets: offsets)
        lists[slot.rawValue] = l
        UserDefaults.standard.set(l, forKey: "fav.\(slot.rawValue)")
    }
}

/// Reads Hugging Face's public model list (no account needed).
enum HubCatalog {
    enum Sort: String, CaseIterable, Identifiable {
        case trending = "trendingScore", downloads, likes
        var id: String { rawValue }
        var label: String {
            switch self {
            case .trending: "Trending"
            case .downloads: "Most downloaded"
            case .likes: "Most liked"
            }
        }
    }

    static func list(_ slot: ModelSlot, sort: Sort, search: String = "", limit: Int = 10) async throws -> [ModelEntry] {
        var c = URLComponents(string: "https://huggingface.co/api/models")!
        var q = slot.query + [URLQueryItem(name: "sort", value: sort.rawValue),
                              URLQueryItem(name: "direction", value: "-1"),
                              URLQueryItem(name: "limit", value: String(limit))]
        // Hugging Face refuses the whole list (HTTP 400) if any expand[] field is unknown: "usedStorage" stopped being
        // accepted in lists, which emptied every list in the picker (found 11-16). Sizes now come from the
        // safetensors summary (parameter counts per number type) or the GGUF parameter count.
        for f in ["downloads", "likes", "safetensors", "gguf"] { q.append(URLQueryItem(name: "expand[]", value: f)) }
        if !search.isEmpty { q.append(URLQueryItem(name: "search", value: search)) }
        c.queryItems = q
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 20
        let (data, response) = try await URLSession(configuration: config).data(from: c.url!)
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            throw URLError(.badServerResponse, userInfo: [NSLocalizedDescriptionKey: "Hugging Face said HTTP \(http.statusCode)."])
        }
        let raw = try JSONSerialization.jsonObject(with: data) as? [[String: Any]] ?? []
        return raw.compactMap { m in
            guard let id = m["id"] as? String else { return nil }
            var size = estimatedBytes(id: id, safetensors: m["safetensors"] as? [String: Any])
            if let params = ((m["gguf"] as? [String: Any])?["total"] as? NSNumber)?.doubleValue {
                size = params * 0.71 + 0.9e9          // one ~5-bit quant plus the vision part, not every quant in the repo
            }
            return ModelEntry(id: id, name: ModelEntry.displayName(id), size: size,
                              downloads: (m["downloads"] as? NSNumber)?.intValue, likes: (m["likes"] as? NSNumber)?.intValue)
        }
    }

    /// Rough download size from Hugging Face's safetensors summary. MLX quantised weights are reported as full
    /// parameter counts, so the bit width in the name decides (4-bit ≈ 0.55 bytes a parameter with scales).
    static func estimatedBytes(id: String, safetensors: [String: Any]?) -> Double? {
        guard let st = safetensors, let total = (st["total"] as? NSNumber)?.doubleValue, total > 0 else { return nil }
        let name = id.lowercased()
        for (bits, perParam) in [("3bit", 0.45), ("4bit", 0.55), ("5bit", 0.68), ("6bit", 0.8), ("8bit", 1.05)]
        where name.contains(bits) { return total * perParam }
        let counts = (st["parameters"] as? [String: Any] ?? [:]).compactMapValues { ($0 as? NSNumber)?.doubleValue }
        let widths: [String: Double] = ["F64": 8, "I64": 8, "F32": 4, "I32": 4, "BF16": 2, "F16": 2, "I16": 2,
                                        "F8_E4M3": 1, "F8_E5M2": 1, "U8": 1, "I8": 1, "U32": 0.55]   // U32 = packed 4-bit
        let bytes = counts.reduce(0.0) { $0 + $1.value * (widths[$1.key] ?? 2) }
        return bytes > 0 ? bytes : total * 2
    }
}

/// Choose a model: favourites, what's popular on Hugging Face right now, search, or an exact ID.
struct ModelPicker: View {
    let slot: ModelSlot
    @ObservedObject private var favorites = Favorites.shared
    @ObservedObject private var rules = Rules.shared
    @State private var sort: HubCatalog.Sort = .trending
    @State private var results: [ModelEntry] = []
    @State private var loading = false
    @State private var loadRun = 0                 // which load() is the latest; an older one's answer is dropped
    @State private var failed: String?
    @State private var search = ""
    @State private var customID = ""
    @State private var installed: [String: Double] = [:]    // models downloaded on the PC (PC slots only)
    @State private var removing: String?                     // asking to confirm "Remove from computer"
    @State private var notice: String?

    private func entry(_ id: String) -> ModelEntry {
        slot.builtIn.first { $0.id == id } ?? results.first { $0.id == id } ?? ModelEntry(id: id, name: ModelEntry.displayName(id))
    }

    private func row(_ m: ModelEntry) -> some View {
        ModelRow(entry: m, slot: slot, inUse: m.id == slot.current, pcSize: slot.onPC ? installed[m.id] : nil)
            .contextMenu {
                if slot.onPC, installed[m.id] != nil {
                    if m.id == slot.current {
                        Text("In use: choose another model before removing this one")
                    } else {
                        Button(role: .destructive) { removing = m.id } label: {
                            Label("Remove from computer", systemImage: "trash")
                        }
                    }
                }
            }
    }

    var body: some View {
        List {
            Section {
                row(entry(slot.current))
            } header: { Text("In use") } footer: { Text(slot.explainer) }

            Section {
                if favorites.ids(slot).isEmpty {
                    Text("Tap the star on any model to keep it here.").foregroundStyle(.secondary)
                }
                ForEach(favorites.ids(slot), id: \.self) { id in
                    Button { slot.current = id } label: { row(entry(id)) }
                        .buttonStyle(.plain)
                }
                .onDelete { favorites.remove(at: $0, slot) }
            } header: { Text("Favorites") } footer: {
                if slot.onPC {
                    Text("“On your computer” models are already downloaded. Press and hold one to remove it from the computer and free the space; choosing it again downloads it again.")
                }
            }

            Section {
                Picker("Sort", selection: $sort) {
                    ForEach(HubCatalog.Sort.allCases) { Text($0.label).tag($0) }
                }
                .pickerStyle(.segmented)
                if loading { HStack { ProgressView(); Text("Loading…").foregroundStyle(.secondary) } }
                if let failed { Text(failed).font(.footnote).foregroundStyle(.secondary) }
                ForEach(results) { m in
                    Button { slot.current = m.id } label: { row(m) }
                        .buttonStyle(.plain)
                }
            } header: {
                Text(search.isEmpty ? "Popular on Hugging Face" : "Results")
            } footer: {
                Text("Models download the first time you use them.")
            }

            Section {
                HStack {
                    TextField("owner/model", text: $customID)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    Button("Use") {
                        let id = customID.trimmingCharacters(in: .whitespacesAndNewlines)
                        guard id.contains("/") else { return }
                        slot.current = id
                        if !favorites.contains(id, slot) { favorites.toggle(id, slot) }
                        customID = ""
                    }
                    .buttonStyle(.borderless)
                }
            } header: { Text("Use a model by ID") }
        }
        .navigationTitle(slot.title)
        .searchable(text: $search, prompt: "Search Hugging Face")
        .onSubmit(of: .search) { Task { await load() } }
        .onChange(of: search) { _, s in if s.isEmpty { Task { await load() } } }
        .onChange(of: sort) { _, _ in Task { await load() } }
        .task { await load() }
        .refreshable { await load() }
        .confirmationDialog("Remove \(removing.map(ModelEntry.displayName) ?? "this model") from your computer?",
                            isPresented: Binding(get: { removing != nil }, set: { if !$0 { removing = nil } }),
                            titleVisibility: .visible) {
            Button("Remove from computer", role: .destructive) {
                guard let id = removing else { return }
                removing = nil
                Task {
                    do { notice = try await Desktop.shared.removeModel(id) }
                    catch { notice = error.localizedDescription }
                    await loadInstalled()
                }
            }
            Button("Cancel", role: .cancel) { removing = nil }
        } message: {
            Text("Frees its space on the computer. Choosing it again later downloads it again.")
        }
        .alert("Fixer", isPresented: Binding(get: { notice != nil }, set: { if !$0 { notice = nil } })) {
            Button("OK", role: .cancel) {}
        } message: { Text(notice ?? "") }
    }

    private func load() async {
        loadRun += 1
        let run = loadRun
        loading = true
        failed = nil
        await loadInstalled()
        guard run == loadRun else { return }           // a newer load has started: its answer is the one to show
        do {
            let found = try await HubCatalog.list(slot, sort: sort, search: search)
            guard run == loadRun else { return }
            results = found
        } catch {
            guard run == loadRun else { return }
            failed = "Couldn't reach Hugging Face. Pull down to try again."
        }
        loading = false
    }

    /// Which models are downloaded on the PC (PC slots only; nothing shown if the PC can't be reached).
    private func loadInstalled() async {
        guard slot.onPC, Desktop.shared.isPaired else { return }
        if let sizes = try? await Desktop.shared.installedModels() { installed = sizes }
    }
}

struct ModelRow: View {
    let entry: ModelEntry
    let slot: ModelSlot
    let inUse: Bool
    var pcSize: Double? = nil          // bytes on the PC when it's downloaded there
    @ObservedObject private var favorites = Favorites.shared

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(entry.name).font(.body).lineLimit(1)
                    if inUse { Image(systemName: "checkmark.circle.fill").foregroundStyle(Color.accentColor) }
                }
                if let note = entry.note { Text(note).font(.caption).foregroundStyle(.secondary) }
                HStack(spacing: 8) {
                    Text(entry.author).lineLimit(1)
                    if let s = entry.size { Text(ByteCountFormatter.string(fromByteCount: Int64(s), countStyle: .file)) }
                    if let d = entry.downloads { Label(d.formatted(.number.notation(.compactName)), systemImage: "arrow.down.circle") }
                    if let l = entry.likes { Label(l.formatted(.number.notation(.compactName)), systemImage: "heart") }
                }
                .font(.caption).foregroundStyle(.secondary).labelStyle(.titleAndIcon)
                if let s = pcSize {
                    Label("On your computer · \(ByteCountFormatter.string(fromByteCount: Int64(s), countStyle: .file))",
                          systemImage: "desktopcomputer")
                        .font(.caption2).foregroundStyle(.green).labelStyle(.titleAndIcon)
                }
                if pcSize == nil, let s = entry.size, s > slot.fitLimit {
                    Text("May be too large for this device").font(.caption2).foregroundStyle(.orange)
                }
            }
            Spacer()
            Button { favorites.toggle(entry.id, slot) } label: {
                Image(systemName: favorites.contains(entry.id, slot) ? "star.fill" : "star")
                    .foregroundStyle(favorites.contains(entry.id, slot) ? Color.yellow : Color.secondary)
            }
            .buttonStyle(.borderless)
        }
        .contentShape(Rectangle())
    }
}
