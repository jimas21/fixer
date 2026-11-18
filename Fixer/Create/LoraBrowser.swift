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
    var pair: Bool
    var id: String { file }

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
        pair = d["pair"] as? Bool ?? false
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

/// One LoRA found by a search. `ref` goes back to the computer to look it up; `thumb` fetches its preview.
struct LoraSearchResult: Identifiable, Hashable {
    let ref: String
    let name: String
    let creator: String
    let size: Double
    let downloads: Int
    let thumb: String?
    var id: String { ref }

    init?(_ d: [String: Any]) {
        guard let ref = d["ref"] as? String, !ref.isEmpty else { return nil }
        self.ref = ref
        name = d["name"] as? String ?? "Add-on"
        creator = d["creator"] as? String ?? ""
        size = (d["size"] as? NSNumber)?.doubleValue ?? 0
        downloads = (d["downloads"] as? NSNumber)?.intValue ?? 0
        thumb = d["thumb"] as? String
    }
}

/// Something to pick from a short list: a file among several, a model family, or one file of a video pair.
struct LoraChoice: Identifiable, Hashable {
    let id: String
    let name: String
    let size: Double

    init?(_ d: [String: Any]) {
        guard let id = d["id"] as? String, !id.isEmpty else { return nil }
        self.id = id
        name = d["name"] as? String ?? id
        size = (d["size"] as? NSNumber)?.doubleValue ?? 0
    }
}

/// What the computer found behind a link or a search result, shown before anything is downloaded.
struct LoraCard {
    let token: String           // handed back to start the download
    let name: String
    let size: Double
    let family: String
    let fits: String            // the model it works with ("" = none here)
    let triggers: [String]
    let warnings: [String]
    let installed: Bool
    let pair: Bool
    let askFamily: Bool         // the computer couldn't tell which model it is for
    let families: [LoraChoice]
    let askRole: Bool           // a video pair whose two files it couldn't tell apart
    let pairFiles: [LoraChoice]

    init?(_ d: [String: Any]) {
        guard let token = d["token"] as? String, !token.isEmpty else { return nil }
        self.token = token
        name = d["name"] as? String ?? "Add-on"
        size = (d["size"] as? NSNumber)?.doubleValue ?? 0
        family = d["family"] as? String ?? "unknown"
        fits = d["fits"] as? String ?? ""
        triggers = d["triggers"] as? [String] ?? []
        warnings = d["warnings"] as? [String] ?? []
        installed = d["installed"] as? Bool ?? false
        pair = d["pair"] as? Bool ?? false
        askFamily = d["ask_family"] as? Bool ?? false
        families = (d["families"] as? [[String: Any]] ?? []).compactMap { LoraChoice($0) }
        askRole = d["ask_role"] as? Bool ?? false
        pairFiles = (d["pair_files"] as? [[String: Any]] ?? []).compactMap { LoraChoice($0) }
    }
}

struct LoraInspection {
    let card: LoraCard?
    let choices: [LoraChoice]   // not empty: several files were found, and one has to be picked first
}

/// What the card screen was opened for: a pasted link, or a search result.
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
    @State private var results: [LoraSearchResult] = []
    @State private var cursor: String?
    @State private var searching = false
    @State private var searchNote: String?
    @State private var searchRun = 0
    @State private var linkText = ""
    @State private var request: LoraRequest?

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
                    Text("Hugging Face").tag("huggingface")
                }
                .pickerStyle(.segmented)
                HStack {
                    Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                    TextField("Search add-ons", text: $query)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .submitLabel(.search)
                        .onSubmit { Task { await search(reset: true) } }
                    if searching { ProgressView() }
                }
            } footer: {
                Text("Showing add-ons made for \(modelName). Others would not work with it.")
            }
            if let searchNote {
                Section {
                    Text(searchNote).font(.footnote).foregroundStyle(.secondary)
                }
            }
            if !results.isEmpty {
                Section {
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
        .task(id: source) { await search(reset: true) }
    }

    private func resultRow(_ r: LoraSearchResult) -> some View {
        HStack(spacing: 12) {
            LoraThumbView(thumb: r.thumb)
            VStack(alignment: .leading, spacing: 2) {
                Text(r.name).lineLimit(2)
                Text(resultDetail(r)).font(.footnote).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(.secondary)
        }
        .frame(minHeight: 56)
        .contentShape(Rectangle())
    }

    private func resultDetail(_ r: LoraSearchResult) -> String {
        var parts: [String] = []
        if !r.creator.isEmpty { parts.append(r.creator) }
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
        do {
            let page = try await Desktop.shared.loraSearch(source: source, family: family, query: words, cursor: from)
            guard run == searchRun else { return }
            if reset {
                results = page.results
            } else {
                let have = Set(results.map(\.id))
                results.append(contentsOf: page.results.filter { !have.contains($0.id) })
            }
            cursor = page.cursor
            searchNote = results.isEmpty ? "Nothing found. Try other words." : nil
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
    @State private var high = ""
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
                        Button {
                            Task { await inspect(choice: c.id) }
                        } label: {
                            LabeledContent(c.name, value: LoraEntry.sizeText(c.size))
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
        .task { await inspect(choice: nil) }
        .onDisappear { adding?.cancel() }
    }

    @ViewBuilder
    private func details(_ c: LoraCard) -> some View {
        Section {
            LabeledContent("Size", value: LoraEntry.sizeText(c.size))
            LabeledContent("Works with", value: fitsText(c))
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
                    ForEach(c.families) { f in Text(f.name).tag(f.id) }
                }
            } footer: {
                Text("Your computer couldn't tell which model this add-on was made for. It only works with that one.")
            }
        }
        if c.askRole {
            Section {
                Picker("First-half file", selection: $high) {
                    Text("Choose…").tag("")
                    ForEach(c.pairFiles) { f in Text(f.name).tag(f.id) }
                }
            } footer: {
                Text("A video add-on comes as two files, one for each half of the drawing. Pick the one for the first half; it is usually named high, H or HN.")
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
            } else if c.installed {
                Text("Already on your computer.").foregroundStyle(.secondary)
            } else {
                Button("Add to your computer") { add(c) }
                    .disabled(!ready(c))
            }
        }
    }

    private func fitsText(_ c: LoraCard) -> String {
        if !c.fits.isEmpty { return c.fits }
        return c.askFamily ? "Not known yet" : LoraEntry.familyName(c.family)
    }

    private func ready(_ c: LoraCard) -> Bool {
        if c.askFamily, family.isEmpty { return false }
        if c.askRole, high.isEmpty { return false }
        return true
    }

    private func inspect(choice: String?) async {
        if card != nil { return }
        loading = true
        note = nil
        do {
            let found = try await Desktop.shared.loraInspect(url: request.link, ref: request.ref, choice: choice)
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
        let chosenFamily = c.askFamily ? family : nil
        let chosenHigh = c.askRole ? high : nil
        adding = Task {
            do {
                let entry = try await Desktop.shared.loraAdd(token: c.token, family: chosenFamily, high: chosenHigh,
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
