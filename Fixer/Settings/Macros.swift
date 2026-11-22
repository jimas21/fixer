import SwiftUI

// MARK: - Macro list

/// Settings → Macros: saved presets (prompt add-on, negative, add-on stack, settings, optional model).
/// Swipe a row to delete it (after a confirm). Saved on the computer.
struct MacroList: View {
    @ObservedObject private var desktop = Desktop.shared
    @EnvironmentObject private var router: AppRouter
    @State private var macros: [Desktop.Macro] = []
    @State private var roles: [Desktop.Role] = []
    @State private var library: [LoraEntry] = []
    @State private var loading = true
    @State private var failed: String?
    @State private var removing: Desktop.Macro?
    @State private var notice: String?

    var body: some View {
        Form {
            if loading {
                Section { ProgressView() }
            } else if let failed {
                Section {
                    Text(failed).foregroundStyle(.secondary)
                    Button("Try again") { Task { await load() } }
                }
            } else {
                Section {
                    NavigationLink {
                        MacroEditor(macro: Desktop.Macro(), library: library, roles: roles, onSaved: { macros = $0 })
                    } label: {
                        Label("New macro", systemImage: "plus")
                    }
                    NavigationLink {
                        MacroRecommend(onAdded: { macros = $0 })
                    } label: {
                        Label("Suggest macros", systemImage: "sparkles")
                    }
                    Text("To save what you have set up in Make, press and hold the Model tile there and choose Save as macro. Swipe a macro right to use it in Make.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                if !macros.isEmpty {
                    Section("Saved macros") {
                        ForEach(macros) { m in
                            NavigationLink {
                                MacroEditor(macro: m, library: library, roles: roles, onSaved: { macros = $0 })
                            } label: {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(m.name.isEmpty ? "Untitled" : m.name)
                                    Text(m.summary).font(.footnote).foregroundStyle(.secondary).lineLimit(1)
                                }
                            }
                            .swipeActions {
                                Button(role: .destructive) { removing = m } label: {
                                    Label("Delete", systemImage: "trash")
                                }
                            }
                            .swipeActions(edge: .leading) {
                                Button { router.useMacro = m.forMake(roles: roles); router.tab = .make } label: {
                                    Label("Use in Make", systemImage: "paintbrush.pointed")
                                }
                                .tint(.accentColor)
                            }
                        }
                    }
                }
                if let notice { Section { Text(notice).font(.footnote).foregroundStyle(.secondary) } }
            }
        }
        .navigationTitle("Macros")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .confirmationDialog("Delete \(removing?.name ?? "this macro")?", isPresented: Binding(
            get: { removing != nil }, set: { if !$0 { removing = nil } }), titleVisibility: .visible) {
            Button("Delete macro", role: .destructive) {
                if let m = removing { Task { await remove(m) } }
                removing = nil
            }
            Button("Cancel", role: .cancel) { removing = nil }
        }
    }

    private func load() async {
        guard desktop.isPaired else { failed = "Your computer isn't connected."; loading = false; return }
        loading = true; failed = nil
        do {
            let r = try await desktop.macrosGet()
            macros = r.macros; roles = r.roles; library = r.library
        } catch {
            failed = "Couldn't reach your computer."
        }
        loading = false
    }

    private func remove(_ m: Desktop.Macro) async {
        do {
            macros = try await desktop.deleteMacro(id: m.id)
            notice = nil
        } catch {
            notice = "Couldn't delete. Try again."
        }
    }
}

// MARK: - Macro editor

struct MacroEditor: View {
    @State var macro: Desktop.Macro
    let library: [LoraEntry]
    var roles: [Desktop.Role] = []
    var onSaved: ([Desktop.Macro]) -> Void
    @EnvironmentObject private var router: AppRouter
    @ObservedObject private var desktop = Desktop.shared
    @Environment(\.dismiss) private var dismiss
    @State private var saving = false
    @State private var notice: String?
    @State private var analyzing = false
    @State private var suggestions: [Desktop.MacroSuggestion] = []
    @State private var showReview = false

    var body: some View {
        Form {
            Section("Name") {
                TextField("Macro name", text: $macro.name)
            }
            Section("Added to the prompt") {
                TextField("Prompt add-on", text: $macro.promptAdd, axis: .vertical).lineLimit(2...8)
            }
            Section("Added to the negative prompt") {
                TextField("Negative add-on", text: $macro.negativeAdd, axis: .vertical).lineLimit(1...6)
            }
            Section("Add-ons (LoRAs)") {
                ForEach($macro.loras) { $slot in loraRow($slot) }
                    .onDelete { macro.loras.remove(atOffsets: $0) }
                Menu {
                    ForEach(choices) { l in
                        Button("\(l.name) · \(LoraEntry.familyName(l.family))") {
                            macro.loras.append(Desktop.MacroLora(file: l.file, strength: l.strength))
                        }
                    }
                } label: {
                    Label("Add an add-on", systemImage: "plus")
                }
                .disabled(choices.isEmpty)
            }
            Section("Settings") {
                OptionalSlider(title: "CFG", value: $macro.cfg, range: 1...15, step: 0.5, start: 5)
                OptionalSlider(title: "Steps", value: $macro.steps, range: 5...80, step: 1, start: 30)
                Picker("Model", selection: Binding(get: { macro.model ?? "" }, set: { macro.model = $0.isEmpty ? nil : $0 })) {
                    Text("Current model").tag("")
                    ForEach(modelChoices, id: \.id) { Text($0.label).tag($0.id) }
                }
                Toggle("18+", isOn: $macro.explicit)
            }
            Section {
                Button {
                    router.useMacro = macro.forMake(roles: roles)
                    router.tab = .make
                } label: { Label("Use in Make", systemImage: "paintbrush.pointed") }
                Button(saving ? "Saving…" : "Save") { Task { await save() } }
                    .disabled(saving || macro.name.trimmingCharacters(in: .whitespaces).isEmpty)
                Button {
                    Task { await analyze() }
                } label: {
                    if analyzing { HStack { ProgressView(); Text("Reviewing…") } }
                    else { Label("Review with AI", systemImage: "sparkles") }
                }
                .disabled(analyzing)
                if let notice { Text(notice).font(.footnote).foregroundStyle(.secondary) }
            }
        }
        .navigationTitle(macro.name.isEmpty ? "New macro" : macro.name)
        .navigationBarTitleDisplayMode(.inline)
        .sheet(isPresented: $showReview) {
            MacroReview(suggestions: suggestions) { picked in
                for s in picked { if let a = s.apply { macro.merge(a) } }
                showReview = false
            }
        }
    }

    /// Installed add-ons that can be added (18+ ones only while 18+ is on).
    private var choices: [LoraEntry] {
        library.filter { !$0.explicit || Rules.shared.explicitNow }
    }

    private var modelChoices: [(id: String, label: String)] {
        var list = Rules.pcImagePresets.map { (id: $0.id, label: $0.label) }
        if let m = macro.model, !list.contains(where: { $0.id == m }) { list.append((id: m, label: ModelEntry.displayName(m))) }
        return list
    }

    @ViewBuilder private func loraRow(_ slot: Binding<Desktop.MacroLora>) -> some View {
        VStack(alignment: .leading) {
            if let role = slot.wrappedValue.role {
                LabeledContent("Role", value: role)
            } else {
                let file = slot.wrappedValue.file ?? ""
                let name = library.first { $0.file == file }?.name ?? LoraEntry.tidy(file)
                LabeledContent(name, value: String(format: "%.2f", slot.wrappedValue.strength))
                Slider(value: slot.strength, in: 0...2, step: 0.05)
            }
        }
    }

    private func save() async {
        saving = true; notice = nil
        do {
            let list = try await desktop.saveMacro(macro)
            onSaved(list)
            dismiss()
        } catch {
            notice = "Couldn't save. Try again."
        }
        saving = false
    }

    private func analyze() async {
        analyzing = true; notice = nil
        do {
            suggestions = try await desktop.analyzeMacro(macro, safe: !Rules.shared.explicitNow)
            if suggestions.isEmpty { notice = "No suggestions: it looks good." } else { showReview = true }
        } catch {
            notice = "Couldn't review it. Try again."
        }
        analyzing = false
    }
}

/// A slider that can be switched off (nil = leave the setting alone).
private struct OptionalSlider: View {
    let title: String
    @Binding var value: Double?
    let range: ClosedRange<Double>
    let step: Double
    let start: Double

    var body: some View {
        VStack(alignment: .leading) {
            Toggle(title, isOn: Binding(get: { value != nil }, set: { value = $0 ? start : nil }))
            if let v = value {
                LabeledContent("Value", value: String(format: step < 1 ? "%.1f" : "%.0f", v))
                Slider(value: Binding(get: { v }, set: { value = $0 }), in: range, step: step)
            }
        }
    }
}

// MARK: - AI review checklist

private struct MacroReview: View {
    let suggestions: [Desktop.MacroSuggestion]
    var onApply: ([Desktop.MacroSuggestion]) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var picked: Set<String> = []

    var body: some View {
        NavigationStack {
            List {
                Section {
                    ForEach(suggestions) { s in
                        Button {
                            guard s.apply != nil else { return }
                            if picked.contains(s.id) { picked.remove(s.id) } else { picked.insert(s.id) }
                        } label: {
                            HStack(alignment: .top, spacing: 12) {
                                if s.apply != nil {
                                    Image(systemName: picked.contains(s.id) ? "checkmark.square.fill" : "square")
                                } else {
                                    Image(systemName: "info.circle").foregroundStyle(.secondary)
                                }
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(s.title).foregroundStyle(.primary)
                                    Text(s.detail).font(.footnote).foregroundStyle(.secondary)
                                    if s.apply == nil {
                                        Text("Advice only").font(.caption2).foregroundStyle(.secondary)
                                    }
                                }
                            }
                        }
                    }
                } footer: {
                    Text("Tick the changes to apply, then tap Apply. Nothing is saved until you save the macro.")
                }
            }
            .navigationTitle("Suggestions")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) { Button("Close") { dismiss() } }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Apply selected") { onApply(suggestions.filter { picked.contains($0.id) }) }
                        .disabled(picked.isEmpty)
                }
                ToolbarItem(placement: .bottomBar) {
                    HStack {
                        Button("Select all") { picked = Set(suggestions.filter { $0.apply != nil }.map { $0.id }) }
                        Spacer()
                        Button("Select none") { picked = [] }
                    }
                }
            }
        }
    }
}

// MARK: - Settings: delete all

/// Settings section with "Delete all macros". Not part of any "clear everything" path: macros are durable.
struct DeleteAllMacrosSection: View {
    @ObservedObject private var desktop = Desktop.shared
    @State private var asking = false
    @State private var notice: String?

    var body: some View {
        Section {
            Button("Delete all macros", role: .destructive) { asking = true }
            if let notice { Text(notice).font(.footnote).foregroundStyle(.secondary) }
        }
        .confirmationDialog("Delete every saved macro? This can't be undone.", isPresented: $asking,
                            titleVisibility: .visible) {
            Button("Delete all macros", role: .destructive) {
                Task {
                    do { try await desktop.deleteAllMacros(); notice = "All macros deleted." }
                    catch { notice = "Couldn't delete. Try again." }
                }
            }
            Button("Cancel", role: .cancel) {}
        }
    }
}

// MARK: - Recommended macros

/// Settings → Macros → Suggest macros: the local AI proposes a few complete macros; each can be added as a new,
/// editable macro (with a fresh id), any, all or none.
struct MacroRecommend: View {
    var onAdded: ([Desktop.Macro]) -> Void
    @ObservedObject private var desktop = Desktop.shared
    @State private var context = ""
    @State private var results: [Desktop.Macro] = []
    @State private var added: Set<String> = []
    @State private var working = false
    @State private var notice: String?

    var body: some View {
        Form {
            Section("What are you going for?") {
                TextField("Optional, e.g. moody street portraits", text: $context, axis: .vertical).lineLimit(1...4)
                Button {
                    Task { await fetch() }
                } label: {
                    if working { HStack { ProgressView(); Text("Thinking…") } }
                    else { Label(results.isEmpty ? "Suggest macros" : "Suggest again", systemImage: "sparkles") }
                }
                .disabled(working)
                if let notice { Text(notice).font(.footnote).foregroundStyle(.secondary) }
            }
            ForEach(results) { r in
                Section(r.name.isEmpty ? "Untitled" : r.name) {
                    if !r.promptAdd.isEmpty { Text(r.promptAdd).font(.footnote).lineLimit(4) }
                    // 11-22: show exactly what the macro will use — the add-ons (with strengths), model and settings
                    // — not just a count, so you can see what you're adopting before you add it.
                    ForEach(r.detailRows, id: \.key) { row in
                        HStack(alignment: .top, spacing: 8) {
                            if row.key.isEmpty {
                                Text(row.value).font(.caption).foregroundStyle(.secondary)
                            } else {
                                Text(row.key).font(.caption).foregroundStyle(.secondary)
                                Spacer(minLength: 8)
                                Text(row.value).font(.caption).multilineTextAlignment(.trailing)
                            }
                        }
                    }
                    if added.contains(r.id) {
                        Label("Added", systemImage: "checkmark").foregroundStyle(.secondary)
                    } else {
                        Button("Add as a new macro") { Task { await add(r) } }
                    }
                }
            }
            if results.count > 1, results.contains(where: { !added.contains($0.id) }) {
                Section { Button("Add all") { Task { for r in results where !added.contains(r.id) { await add(r) } } } }
            }
        }
        .navigationTitle("Suggest macros")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func fetch() async {
        working = true; notice = nil
        do {
            results = try await desktop.recommendMacros(context: context, safe: !Rules.shared.explicitNow)
            added = []
            if results.isEmpty { notice = "No suggestions this time." }
        } catch {
            notice = "Couldn't get suggestions. Try again."
        }
        working = false
    }

    private func add(_ m: Desktop.Macro) async {
        do {
            onAdded(try await desktop.saveMacro(m))
            added.insert(m.id)
        } catch {
            notice = "Couldn't add it. Try again."
        }
    }
}

// MARK: - Hand-off to Make

extension Desktop.Macro {
    /// What Make needs to load this macro: add-on roles turned into the installed files they point at (a role that is
    /// off or missing is left out). Make itself drops add-ons that don't fit its model or the 18+ switch.
    func forMake(roles: [Desktop.Role]) -> MacroApply {
        var picks: [MacroApply.Lora] = []
        for l in loras {
            if let r = l.role {
                if let role = roles.first(where: { $0.name == r && $0.enabled }), !role.file.isEmpty {
                    picks.append(MacroApply.Lora(file: role.file, strength: role.strength))
                }
            } else if let f = l.file, !f.isEmpty {
                picks.append(MacroApply.Lora(file: f, strength: l.strength))
            }
        }
        return MacroApply(name: name, promptAdd: promptAdd, negativeAdd: negativeAdd, loras: picks,
                          cfg: cfg, steps: steps, model: model, explicit: explicit)
    }
}
