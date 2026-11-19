import SwiftUI
import UIKit

// MARK: - What the computer says about LoRAs

/// One LoRA in the computer's library. `file` is its id: the name the phone sends back with a job (for a video pair,
/// the first-half file stands for both).
struct LoraEntry: Identifiable, Hashable {
    let file: String
    var name: String
    var family: String          // sdxl, chroma, qwen21, qwen-edit, wan22 or unknown
    var size: Double            // bytes (a pair: both files)
    var triggers: [String]
    var strength: Double        // the strength it is used at unless a run says otherwise
    var alwaysOn: Bool          // used with every picture or video of its model, the chat's included
    var source: String
    var pair: Bool              // one file of a video pair
    var role: String?           // "high" or "low" for a file of a pair
    var id: String { file }

    /// The five model families a LoRA can be filed under.
    static let families = ["sdxl", "chroma", "qwen21", "qwen-edit", "wan22"]

    /// The library as the rows to show. The second-half file of a video pair is left out: the first-half file
    /// stands for both, and the computer keeps the two in step.
    static func rows(_ raw: Any?) -> [LoraEntry] {
        let all = (raw as? [[String: Any]] ?? []).compactMap { LoraEntry($0) }
        return all.filter { !($0.pair && $0.role == "low") }
    }

    init?(_ d: [String: Any]) {
        guard let file = d["file"] as? String, !file.isEmpty else { return nil }
        self.file = file
        let given = (d["name"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        name = given.isEmpty ? LoraEntry.tidy(file) : given
        family = d["family"] as? String ?? "unknown"
        size = (d["size"] as? NSNumber)?.doubleValue ?? 0
        triggers = d["triggers"] as? [String] ?? []
        strength = (d["strength"] as? NSNumber)?.doubleValue ?? 1
        alwaysOn = d["always_on"] as? Bool ?? false
        source = d["source"] as? String ?? ""
        if let pairID = d["pair"] as? String {
            pair = !pairID.isEmpty
        } else {
            pair = false
        }
        role = d["role"] as? String
    }

    /// A file an older computer listed by name only.
    init(file: String) {
        self.file = file
        name = LoraEntry.tidy(file)
        family = "unknown"
        size = 0
        triggers = []
        strength = 1
        alwaysOn = false
        source = ""
        pair = false
        role = nil
    }

    /// A file name as a readable name.
    static func tidy(_ file: String) -> String {
        var s = file
        if s.lowercased().hasSuffix(".safetensors") { s = String(s.dropLast(".safetensors".count)) }
        return s.replacingOccurrences(of: "_", with: " ").replacingOccurrences(of: "-", with: " ")
    }

    /// The model a family of LoRAs works with, in plain words.
    static func familyName(_ family: String) -> String {
        switch family {
        case "sdxl": return "Juggernaut XL"
        case "chroma": return "Chroma1 HD"
        case "qwen21": return "Qwen-Image 2.1"
        case "qwen-edit": return "Qwen Edit"
        case "wan22": return "Wan 2.2 video"
        default: return "Not sorted yet"
        }
    }

    static func sizeText(_ bytes: Double) -> String {
        if bytes <= 0 { return "Size unknown" }
        if bytes >= 1e9 { return String(format: "%.1f GB", bytes / 1e9) }
        return String(format: "%.0f MB", Swift.max(1, bytes / 1e6))
    }
}

/// One LoRA found by a search. `ref` is the computer's id for it (it goes back to look it up); `thumb` is the
/// address of its preview, which the computer fetches.
struct LoraSearchResult: Identifiable, Hashable {
    let ref: String
    let name: String
    let size: Double
    let downloads: Int
    let thumb: String?
    let base: String            // Civitai's base-model label (e.g. "Chroma", "Pony", "Wan Video 2.2 T2V-A14B")
    let fits: Bool              // whether it matches one of the engines being searched
    var id: String { ref }

    init?(_ d: [String: Any]) {
        guard let ref = d["source_id"] as? String, !ref.isEmpty else { return nil }
        self.ref = ref
        let given = d["name"] as? String ?? ""
        name = given.isEmpty ? "Add-on" : given
        size = ((d["size_mb"] as? NSNumber)?.doubleValue ?? 0) * 1_048_576
        downloads = (d["downloads"] as? NSNumber)?.intValue ?? 0
        thumb = d["thumb"] as? String
        base = d["base"] as? String ?? ""
        fits = d["fits"] as? Bool ?? true
    }

    /// A short engine badge from the base-model label.
    var engineLabel: String {
        let b = base.lowercased()
        if b.contains("chroma") { return "Chroma" }
        if b.contains("wan") { return "Wan 2.2" }
        if b.contains("qwen") { return "Qwen" }
        if ["sdxl", "pony", "illustrious", "noobai"].contains(where: b.contains) { return "SDXL" }
        return base
    }
}

/// One file among several found behind a link. `id` is the link that leads to just that file.
struct LoraChoice: Identifiable, Hashable {
    let id: String
    let name: String
}

/// What the computer found behind a link or a search result, shown before anything is downloaded.
struct LoraCard {
    let name: String
    let size: Double            // bytes; 0 when the source doesn't say
    let family: String          // "" when the computer couldn't tell, or when it fits no model here
    let fits: String            // the model it works with ("" = none here)
    let triggers: [String]
    let warnings: [String]
    let files: [String]
    let askFamily: Bool         // the computer couldn't tell which model it is for: the owner picks

    /// It was made for a model this computer doesn't have: there is nothing to add.
    var refused: Bool { family.isEmpty && !askFamily }

    init?(_ d: [String: Any]) {
        guard let name = d["name"] as? String else { return nil }
        self.name = name.isEmpty ? "Add-on" : name
        size = ((d["size_mb"] as? NSNumber)?.doubleValue ?? 0) * 1_048_576
        family = d["family"] as? String ?? ""
        fits = d["fits"] as? String ?? ""
        triggers = d["triggers"] as? [String] ?? []
        var notes: [String] = []
        if let one = d["warning"] as? String, !one.isEmpty { notes.append(one) }
        warnings = notes
        files = d["files"] as? [String] ?? []
        askFamily = d["needs_pick"] as? Bool ?? false
    }
}

struct LoraInspection {
    let card: LoraCard?
    let choices: [LoraChoice]   // not empty: several files were found, and one has to be picked first
}

/// What the card screen was opened for: a pasted link, or a search result (`ref`, the computer's id for it).
struct LoraRequest: Hashable {
    var link: String?
    var ref: String?
    var title: String
}

// MARK: - Add a LoRA

/// "Add a LoRA": browse Civitai or Hugging Face for add-ons made for the model in use, or paste a link. Both lead to
/// the same card, which says what the add-on is and what it fits before anything is downloaded. The searching and
/// downloading happen on the computer; the phone keeps no search words, links or previews.
struct LoraBrowser: View {
    let family: String
    let modelName: String
    var onAdded: (LoraEntry) -> Void = { _ in }
    @Environment(\.dismiss) private var dismiss
    @State private var tab = "browse"
    @State private var source = "civitai"
    @State private var query = ""
    @State private var engines: Set<String>        // which engines to search (defaults to the one in use)
    @State private var category = ""               // a chosen category tag ("" = none)
    @State private var sort = "popular"            // popular | trending | rated | new
    @State private var results: [LoraSearchResult] = []
    @State private var cursor: String?
    @State private var searching = false
    @State private var searchNote: String?
    @State private var searchRun = 0
    @State private var loadedKey: String?          // the facets the list on screen was loaded for
    @State private var linkText = ""
    @State private var request: LoraRequest?

    /// The categories to browse: the label shown, and the Civitai tag sent.
    private let categories = [("Characters", "character"), ("Style", "style"), ("Concept", "concept"),
                              ("Clothing", "clothing"), ("Poses", "poses"), ("Background", "background")]

    init(family: String, modelName: String, onAdded: @escaping (LoraEntry) -> Void = { _ in }) {
        self.family = family
        self.modelName = modelName
        self.onAdded = onAdded
        _engines = State(initialValue: family.isEmpty ? [] : [family])
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                Picker("How to find it", selection: $tab) {
                    Text("Browse").tag("browse")
                    Text("Paste link").tag("paste")
                }
                .pickerStyle(.segmented)
                .padding(.horizontal, 16)
                .padding(.vertical, 8)
                if tab == "browse" {
                    browsePage
                } else {
                    pastePage
                }
            }
            .navigationTitle("Add a LoRA")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() } }
            }
            .navigationDestination(item: $request) { r in
                LoraCardView(request: r, onAdded: onAdded)
            }
        }
    }

    // MARK: browse

    private var browsePage: some View {
        List {
            Section {
                Picker("From", selection: $source) {
                    Text("Civitai").tag("civitai")
                    Text("Hugging Face").tag("hf")
                }
                .pickerStyle(.segmented)
                HStack {
                    Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                    TextField("Search add-ons", text: $query)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .submitLabel(.search)
                        .onSubmit { category = ""; Task { await search(reset: true) } }
                    if !query.isEmpty {
                        Button { query = ""; Task { await search(reset: true) } } label: {
                            Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
                        }
                        .buttonStyle(.plain)
                    }
                    if searching { ProgressView() }
                }
            } footer: {
                Text(source == "hf"
                     ? "Hugging Face add-ons for \(modelName)."
                     : "Browse the Civitai community — pick engines, a category, or search. Each add-on is checked to fit before it downloads.")
            }
            if source == "civitai" {
                Section("Engines") { engineChips }
                Section("Sort") {
                    Picker("Sort", selection: $sort) {
                        Text("Popular").tag("popular")
                        Text("Trending").tag("trending")
                        Text("Top rated").tag("rated")
                        Text("Newest").tag("new")
                    }
                    .pickerStyle(.segmented)
                }
                Section("Categories") { categoryChips }
            }
            if let searchNote {
                Section {
                    Text(searchNote).font(.footnote).foregroundStyle(.secondary)
                }
            }
            if !results.isEmpty {
                Section("Add-ons") {
                    ForEach(results) { r in
                        Button {
                            request = LoraRequest(link: nil, ref: r.ref, title: r.name)
                        } label: {
                            resultRow(r)
                        }
                        .buttonStyle(.plain)
                        .onAppear {
                            if r.id == results.last?.id { Task { await search(reset: false) } }
                        }
                    }
                }
            }
        }
        .task(id: facetKey) {
            if loadedKey == facetKey, !results.isEmpty { return }      // back from a details page: keep the list
            await search(reset: true)
            if !Task.isCancelled { loadedKey = facetKey }
        }
    }

    /// A change to the engines, category, sort or source re-runs the search from the first page.
    private var facetKey: String {
        source + "|" + engines.sorted().joined(separator: ",") + "|" + category + "|" + sort
    }

    private var engineChips: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(LoraEntry.families, id: \.self) { f in
                    facetChip(Self.engineShort(f), on: engines.contains(f)) {
                        if engines.contains(f) {
                            if engines.count > 1 { engines.remove(f) }   // keep at least one engine selected
                        } else {
                            engines.insert(f)
                        }
                    }
                }
            }
            .padding(.vertical, 2)
        }
        .listRowInsets(EdgeInsets(top: 6, leading: 16, bottom: 6, trailing: 16))
    }

    private var categoryChips: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                facetChip("All", on: category.isEmpty) { category = "" }
                ForEach(categories.indices, id: \.self) { i in
                    let c = categories[i]
                    facetChip(c.0, on: category == c.1) {
                        query = ""
                        category = (category == c.1) ? "" : c.1
                    }
                }
            }
            .padding(.vertical, 2)
        }
        .listRowInsets(EdgeInsets(top: 6, leading: 16, bottom: 6, trailing: 16))
    }

    private func facetChip(_ label: String, on: Bool, _ tap: @escaping () -> Void) -> some View {
        Button(action: tap) {
            Text(label)
                .font(.footnote.weight(.medium))
                .padding(.horizontal, 12).padding(.vertical, 7)
                .background(Capsule().fill(on ? Fx.accent.opacity(0.18) : Fx.tile))
                .overlay(Capsule().strokeBorder(on ? Fx.accent : Fx.hairline, lineWidth: on ? 1 : 0.5))
                .foregroundStyle(on ? Fx.accent : Fx.secondaryText)
        }
        .buttonStyle(.plain)
    }

    static func engineShort(_ f: String) -> String {
        switch f {
        case "sdxl": return "SDXL"
        case "chroma": return "Chroma"
        case "qwen21": return "Qwen"
        case "qwen-edit": return "Qwen Edit"
        case "wan22": return "Wan 2.2"
        default: return f
        }
    }

    private func resultRow(_ r: LoraSearchResult) -> some View {
        HStack(spacing: 12) {
            LoraThumbView(thumb: r.thumb)
            VStack(alignment: .leading, spacing: 2) {
                Text(r.name).lineLimit(2)
                HStack(spacing: 6) {
                    if engines.count > 1, !r.engineLabel.isEmpty {
                        Text(r.engineLabel)
                            .font(.caption2.weight(.semibold))
                            .padding(.horizontal, 6).padding(.vertical, 2)
                            .background(Capsule().fill(Fx.tile))
                            .foregroundStyle(Fx.secondaryText)
                    }
                    Text(resultDetail(r)).font(.footnote).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.secondary)
        }
        .frame(minHeight: 56)
        .contentShape(Rectangle())
    }

    private func resultDetail(_ r: LoraSearchResult) -> String {
        var parts: [String] = []
        if r.size > 0 { parts.append(LoraEntry.sizeText(r.size)) }
        if r.downloads > 0 { parts.append("\(r.downloads) downloads") }
        return parts.joined(separator: " · ")
    }

    /// The first page (`reset`) or the next one. A page that comes back after a newer search started is dropped.
    private func search(reset: Bool) async {
        if !reset, cursor == nil || searching { return }
        searchRun += 1
        let run = searchRun
        let from = reset ? nil : cursor
        let words = query.trimmingCharacters(in: .whitespacesAndNewlines)
        searching = true
        if reset { searchNote = nil }
        let (apiSort, period): (String, String?) =
            sort == "trending" ? ("popular", "week") :
            sort == "rated" ? ("rated", nil) :
            sort == "new" ? ("new", nil) : ("popular", nil)
        do {
            let page = try await Desktop.shared.loraSearch(
                source: source, family: family, query: words, cursor: from,
                families: engines.isEmpty ? nil : Array(engines),
                sort: apiSort, period: period, category: category.isEmpty ? nil : category)
            guard run == searchRun else { return }
            if reset {
                results = page.results
            } else {
                let have = Set(results.map(\.id))
                results.append(contentsOf: page.results.filter { !have.contains($0.id) })
            }
            cursor = page.cursor
            searchNote = results.isEmpty ? "Nothing here. Try other engines, a category, or words." : nil
        } catch {
            guard run == searchRun else { return }
            if !Task.isCancelled { searchNote = error.localizedDescription }
        }
        searching = false
    }

    // MARK: paste

    private var pastePage: some View {
        let trimmed = linkText.trimmingCharacters(in: .whitespacesAndNewlines)
        return Form {
            Section {
                TextField("https://…", text: $linkText, axis: .vertical)
                    .lineLimit(1...4)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                Button {
                    if let s = UIPasteboard.general.string { linkText = s }
                } label: {
                    Label("Paste", systemImage: "doc.on.clipboard")
                }
            } footer: {
                Text("A Civitai or Hugging Face page, or a direct link to a .safetensors file.")
            }
            Section {
                Button("Look it up") {
                    request = LoraRequest(link: trimmed, ref: nil, title: "Add-on")
                }
                .disabled(trimmed.isEmpty)
            }
        }
    }
}

/// A search result's preview, fetched through the computer when its row comes on screen.
private struct LoraThumbView: View {
    let thumb: String?
    @State private var image: UIImage?

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Fx.tile)
            if let image {
                Image(uiImage: image).resizable().scaledToFill()
            } else {
                Image(systemName: "photo").foregroundStyle(Fx.secondaryText)
            }
        }
        .frame(width: 56, height: 56)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .task(id: thumb) {
            image = nil
            guard let thumb else { return }
            let loaded = try? await Desktop.shared.loraThumb(thumb)
            image = loaded
        }
        .accessibilityHidden(true)
    }
}

// MARK: - The confirmation card

/// What a link or a search result turned out to be: name, size, the model it fits, its trigger words and any
/// warnings, with Add, a progress bar and Cancel. Nothing is downloaded until Add is tapped.
struct LoraCardView: View {
    let request: LoraRequest
    var onAdded: (LoraEntry) -> Void
    @State private var card: LoraCard?
    @State private var choices: [LoraChoice] = []
    @State private var loading = true
    @State private var note: String?
    @State private var family = ""
    @State private var pickedLink: String?          // the one file chosen among several
    @State private var adding: Task<Void, Never>?
    @State private var progress: Double = 0
    @State private var status: String?
    @State private var added: LoraEntry?

    var body: some View {
        Form {
            if loading {
                HStack(spacing: 10) {
                    ProgressView()
                    Text("Looking it up…").foregroundStyle(.secondary)
                }
            }
            if let note {
                Text(note).font(.footnote).foregroundStyle(Fx.danger)
            }
            if card == nil, !choices.isEmpty {
                Section("Which file?") {
                    ForEach(choices) { c in
                        Button(c.name) {
                            pickedLink = c.id
                            Task { await inspect() }
                        }
                    }
                }
            }
            if let card {
                details(card)
                questions(card)
                actions(card)
            }
        }
        .navigationTitle(card?.name ?? request.title)
        .navigationBarTitleDisplayMode(.inline)
        .task { await inspect() }
        .onDisappear { adding?.cancel() }
    }

    @ViewBuilder
    private func details(_ c: LoraCard) -> some View {
        Section {
            LabeledContent("Size", value: LoraEntry.sizeText(c.size))
            LabeledContent("Works with", value: fitsText(c))
            if c.files.count > 1 {
                LabeledContent("Files", value: String(c.files.count))
            }
            if !c.triggers.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Trigger words")
                    Text(c.triggers.joined(separator: ", ")).font(.footnote).foregroundStyle(.secondary)
                }
            }
        } footer: {
            if !c.triggers.isEmpty {
                Text("Trigger words are added to your prompt for you whenever this add-on is used.")
            }
        }
        if !c.warnings.isEmpty {
            Section("Before you add it") {
                ForEach(c.warnings, id: \.self) { w in
                    Label(w, systemImage: "exclamationmark.triangle").font(.footnote)
                }
            }
        }
    }

    @ViewBuilder
    private func questions(_ c: LoraCard) -> some View {
        if c.askFamily {
            Section {
                Picker("Made for", selection: $family) {
                    Text("Choose…").tag("")
                    ForEach(LoraEntry.families, id: \.self) { f in Text(LoraEntry.familyName(f)).tag(f) }
                }
            } footer: {
                Text("Your computer couldn't tell which model this add-on was made for. It only works with that one.")
            }
        }
    }

    @ViewBuilder
    private func actions(_ c: LoraCard) -> some View {
        Section {
            if let added {
                Label("Added: \(added.name)", systemImage: "checkmark.circle.fill")
            } else if adding != nil {
                VStack(alignment: .leading, spacing: 6) {
                    ProgressView(value: Swift.min(1, Swift.max(0, progress)))
                    Text(status ?? "Downloading…").font(.footnote).foregroundStyle(.secondary)
                }
                Button("Cancel", role: .cancel) { cancelAdd() }
            } else if c.refused {
                Text("It was made for a model that isn't on your computer, so it can't be added.")
                    .foregroundStyle(.secondary)
            } else {
                Button("Add to your computer") { add(c) }
                    .disabled(!ready(c))
            }
        }
    }

    private func fitsText(_ c: LoraCard) -> String {
        if !c.fits.isEmpty { return c.fits }
        if c.askFamily { return "Not known yet" }
        return c.family.isEmpty ? "No model on your computer" : LoraEntry.familyName(c.family)
    }

    private func ready(_ c: LoraCard) -> Bool {
        if c.refused { return false }
        if c.askFamily, family.isEmpty { return false }
        return true
    }

    /// What to ask the computer about: the one file picked among several, else what the screen was opened for.
    private var askLink: String? { pickedLink ?? request.link }
    private var askRef: String? { pickedLink == nil ? request.ref : nil }

    private func inspect() async {
        if card != nil { return }
        loading = true
        note = nil
        do {
            let found = try await Desktop.shared.loraInspect(link: askLink, ref: askRef)
            card = found.card
            choices = found.choices
            if found.card == nil, found.choices.isEmpty { note = "Nothing to add was found there." }
        } catch {
            if !Task.isCancelled { note = error.localizedDescription }
        }
        loading = false
    }

    private func add(_ c: LoraCard) {
        note = nil
        progress = 0
        status = "Starting…"
        // The family always goes along: the computer keeps the two files of a video add-on as a pair only then.
        let chosenFamily = c.askFamily ? family : c.family
        let link = askLink, ref = askRef
        adding = Task {
            do {
                let entry = try await Desktop.shared.loraAdd(link: link, ref: ref, family: chosenFamily,
                                                             status: { s in status = s },
                                                             progress: { v in progress = v })
                added = entry
                onAdded(entry)
            } catch {
                if !Task.isCancelled { note = error.localizedDescription }
            }
            adding = nil
        }
    }

    private func cancelAdd() {
        adding?.cancel()
        adding = nil
        status = nil
    }
}
