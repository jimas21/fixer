import os
import SwiftUI

struct SettingsScreen: View {
    @ObservedObject var chat: Chat
    var inTab = true
    @ObservedObject private var rules = Rules.shared
    @ObservedObject private var desktop = Desktop.shared
    @ObservedObject private var favorites = Favorites.shared
    @Environment(\.dismiss) private var dismiss
    @State private var connection: String?

    private var chatSlot: ModelSlot { ModelSlot.chat() }
    private var imageSlot: ModelSlot { ModelSlot.image() }

    private func name(_ slot: ModelSlot) -> String {
        slot.builtIn.first { $0.id == slot.current }?.name ?? ModelEntry.displayName(slot.current)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Quick") {
                    NavigationLink { InstructionsEditor() } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Custom instructions")
                            Text(rules.instructions.isEmpty ? "None" : rules.instructions)
                                .font(.footnote).foregroundStyle(.secondary).lineLimit(1)
                        }
                    }
                    NavigationLink { ModelPicker(slot: chatSlot) } label: {
                        LabeledContent("Chat model", value: name(chatSlot))
                    }
                    if let link = desktop.link {
                        Toggle("Use \(link.name)", isOn: $rules.usePC)
                    }
                    if desktop.isOn {
                        NavigationLink { QueueScreen() } label: { Label("Your computer's work", systemImage: "list.number") }
                    }
                }

                Section {
                    NavigationLink("Image quality") { ImageSettings() }
                    NavigationLink("Content filter") { FilterSettings() }
                } header: {
                    Text("Pictures and video")
                } footer: {
                    Text(desktop.isOn ? "Running on \(desktop.link?.name ?? "your computer")." : "Running on this iPhone.")
                }

                Section("Privacy") {
                    NavigationLink { ClearEverythingPage(chat: chat) } label: {
                        Label("Clear everything", systemImage: "trash").foregroundStyle(.red)
                    }
                }

                Section {
                    NavigationLink("Responses") { ResponseSettings() }
                    NavigationLink("Web search") { SearchSettings() }
                    Toggle("Ask before making a video", isOn: $rules.askBeforeVideo)
                    if !desktop.isOn {
                        NavigationLink { ModelPicker(slot: imageSlot) } label: {
                            LabeledContent("iPhone image model", value: name(imageSlot))
                        }
                    }
                    if desktop.isOn {
                        NavigationLink { ModelPicker(slot: .desktopVideo) } label: {
                            LabeledContent("Video model", value: name(.desktopVideo))
                        }
                        NavigationLink { ModelsOnComputer() } label: {
                            Label("Models on your computer", systemImage: "externaldrive")
                        }
                    }
                    desktopDetails
                    if !desktop.isOn {
                        NavigationLink("Storage") { StorageSettings() }
                    }
                } header: {
                    Text("Advanced")
                }

                Section {
                    LabeledContent("Version", value: Self.version)
                }
            }
            .tabBarClearance()
            .navigationTitle("Settings")
            .toolbar {
                if !inTab { ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() } } }
            }
        }
    }

    /// Fixer Desktop: connection details. The on/off switch is under Quick.
    @ViewBuilder private var desktopDetails: some View {
        if desktop.link != nil {
            Button("Check connection") {
                connection = "Checking…"
                Task {
                    do { connection = try await desktop.status() } catch { connection = error.localizedDescription }
                }
            }
            if let connection { Text(connection).font(.footnote).foregroundStyle(.secondary) }
            Button("Disconnect from computer", role: .destructive) { desktop.unpair(); connection = nil }
        } else {
            VStack(alignment: .leading, spacing: 6) {
                Text("Fixer Desktop: not connected")
                Text("On your computer, open Fixer Desktop from the system tray, choose Pair a Phone, and scan the code with your iPhone camera. It runs chat and image models on your Windows PC; requests are end-to-end encrypted and nothing is stored on the computer.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
        }
    }

    static var version: String {
        let v = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "1.0"
        let b = Bundle.main.infoDictionary?["CFBundleVersion"] as? String ?? ""
        return b.isEmpty ? v : "\(v) (\(b))"
    }
}

struct InstructionsEditor: View {
    @ObservedObject private var rules = Rules.shared
    var body: some View {
        Form {
            Section {
                TextEditor(text: $rules.instructions).frame(minHeight: 220)
            } footer: {
                Text("Tell Fixer how to respond: personality, tone, length, what to call you, anything it should always keep in mind.")
            }
            Section {
                Button("Reset to default personality") { rules.instructions = Rules.defaultPersonality }
                    .disabled(rules.instructions == Rules.defaultPersonality)
            }
        }
        .navigationTitle("Custom instructions")
    }
}

struct ResponseSettings: View {
    @ObservedObject private var rules = Rules.shared
    var body: some View {
        Form {
            Section {
                VStack(alignment: .leading) {
                    Slider(value: $rules.temperature, in: 0...1.5, step: 0.1)
                    HStack { Text("Precise"); Spacer(); Text("Creative") }.font(.caption).foregroundStyle(.secondary)
                }
            } header: { Text("Creativity") }

            Section {
                Picker("Length", selection: $rules.maxTokens) {
                    Text("Short").tag(400)
                    Text("Medium").tag(800)
                    Text("Long").tag(1600)
                    Text("Very long").tag(3200)
                    if ![400, 800, 1600, 3200].contains(rules.maxTokens) { Text("Custom").tag(rules.maxTokens) }
                }
            } header: { Text("Maximum response length") }

            Section {
                Toggle("Use my words for pictures", isOn: $rules.useMyWords)
            } footer: {
                Text("On: pictures and videos are made from exactly what you typed. Off: Fixer adds detail to your words, but if it ever adds clothing or covering you didn't ask for, your own words are used instead.")
            }
        }
        .navigationTitle("Responses")
    }
}

struct ImageSettings: View {
    @ObservedObject private var rules = Rules.shared
    @ObservedObject private var desktop = Desktop.shared
    var body: some View {
        Form {
            if desktop.isOn {
                Section {
                    Stepper("Steps: \(rules.pcSteps)", value: $rules.pcSteps, in: 1...60)
                    Stepper("Prompt strength: \(rules.pcGuidance, specifier: "%.1f")", value: $rules.pcGuidance, in: 0...10, step: 0.5)
                    Picker("Size", selection: $rules.pcSize) {
                        Text("Small (768)").tag(768); Text("Standard (1024)").tag(1024); Text("Large (1536)").tag(1536)
                    }
                } footer: {
                    Text("Steps and prompt strength are set for each model when you choose it.")
                }
            } else {
                Section {
                    Stepper("Steps: \(rules.steps)", value: $rules.steps, in: 8...50)
                    Stepper("Prompt strength: \(rules.guidance, specifier: "%.1f")", value: $rules.guidance, in: 1...15, step: 0.5)
                }
            }
            Section {
                VStack(alignment: .leading) {
                    Slider(value: $rules.editStrength, in: 0.1...1, step: 0.05)
                    HStack { Text("Subtle"); Spacer(); Text("Bold") }.font(.caption).foregroundStyle(.secondary)
                }
            } header: {
                Text("Edit strength")
            } footer: {
                Text("How much a redraw changes the original photo.")
            }
        }
        .navigationTitle("Image quality")
    }
}

struct FilterSettings: View {
    @ObservedObject private var rules = Rules.shared
    var body: some View {
        Form {
            Section("Editing your photos") {
                ThresholdSlider(title: "Swimwear & lingerie", value: $rules.editRevealing, canTurnOff: true)
                ThresholdSlider(title: "Nudity", value: $rules.editExplicit, canTurnOff: true, cap: Rules.editExplicitCap)
            }
            Section {
                ThresholdSlider(title: "Swimwear & lingerie", value: $rules.newRevealing, canTurnOff: true)
                ThresholdSlider(title: "Nudity", value: $rules.newExplicit, canTurnOff: true)
            } header: {
                Text("Creating new images")
            } footer: {
                Text("Applies to everything Fixer makes or redraws, on this iPhone and on your computer, including Make and videos (3 frames checked). Remove, cut out and upscale make nothing new and aren't checked. Slide right to filter less; all the way right turns a filter off.")
            }
        }
        .navigationTitle("Content filter")
    }
}

struct SearchSettings: View {
    @ObservedObject private var rules = Rules.shared
    var body: some View {
        Form {
            Section {
                TextField("Server address", text: $rules.searchURL)
                    .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                Stepper("Results to read: \(rules.searchResults)", value: $rules.searchResults, in: 3...15)
            } footer: {
                Text("Fixer searches the web when it needs current information, using your SearXNG server. With Fixer Desktop on, searches go through your computer.")
            }
        }
        .navigationTitle("Web search")
    }
}

struct StorageSettings: View {
    @ObservedObject private var store = ModelStore.shared
    @ObservedObject private var brain = Brain.shared
    @State private var error: String?

    var body: some View {
        Form {
            Section {
                HStack {
                    Text("Chat model")
                    Spacer()
                    switch brain.state {
                    case .downloading(let p): ProgressView(value: p).frame(width: 100)
                    case .loading: Text("Loading…").foregroundStyle(.secondary)
                    case .ready: Text("Ready").foregroundStyle(.secondary)
                    case .failed: Text("Failed").foregroundStyle(.red)
                    case .idle:
                        Button("Download") { Task { do { try await brain.load() } catch { self.error = error.localizedDescription } } }
                    }
                }
                ForEach(CoreModel.allCases) { m in
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(m.title)
                            Text("\(m.approxMB) MB").font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if let p = store.progress[m] {
                            ProgressView(value: p).frame(width: 100)
                        } else if store.isInstalled(m) {
                            Button("Delete", role: .destructive) { store.delete(m) }
                        } else {
                            Button("Download") {
                                Task { do { try await store.ensure(m) } catch { self.error = error.localizedDescription } }
                            }
                        }
                    }
                }
            } header: {
                Text("On this iPhone")
            } footer: {
                Text("Models download once and then work offline. Available memory: \(ByteCountFormatter.string(fromByteCount: Int64(os_proc_available_memory()), countStyle: .memory)).")
            }
        }
        .navigationTitle("Storage")
        .alert("Download failed", isPresented: Binding(get: { error != nil }, set: { if !$0 { error = nil } })) {
            Button("OK", role: .cancel) {}
        } message: { Text(error ?? "") }
    }
}

/// Filter strength. Far right turns the filter off when `canTurnOff`.
struct ThresholdSlider: View {
    let title: String
    @Binding var value: Double
    let canTurnOff: Bool
    var cap: Double = 1.0

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack {
                Text(title)
                Spacer()
                Text(label).font(.footnote).foregroundStyle(.secondary)
            }
            Slider(value: Binding(get: { value }, set: { v in
                // The far right is exactly Off (1.0): step arithmetic can stop a hair short of it.
                value = canTurnOff && v >= cap - 0.025 ? cap : v
            }), in: 0.3...(canTurnOff ? cap : min(cap, 0.95)), step: 0.05)
        }
    }

    private var label: String {
        if canTurnOff && FilterSetting.off(value) { return "Off" }
        switch value {
        case ..<0.5: return "Strict"
        case ..<0.75: return "Standard"
        default: return "Relaxed"
        }
    }
}

/// One model on the computer, from the `installed` op.
struct InstalledModel: Identifiable, Hashable {
    let id: String
    let name: String
    let kind: String        // "picture", "video", "chat", "other"
    let sizeGB: Double
    let inUse: Bool

    init?(_ d: [String: Any]) {
        guard let id = d["id"] as? String, !id.isEmpty else { return nil }
        self.id = id
        name = d["name"] as? String ?? id
        kind = d["kind"] as? String ?? "other"
        sizeGB = (d["size_gb"] as? NSNumber)?.doubleValue ?? 0
        inUse = d["in_use"] as? Bool ?? false
    }
}

/// Settings → Models on your computer: the picture, video and chat models downloaded on the PC, each removable.
/// (LoRAs and the Civitai key are added here once the LoRA library exists.)
struct ModelsOnComputer: View {
    @ObservedObject private var desktop = Desktop.shared
    @State private var models: [InstalledModel] = []
    @State private var loading = true
    @State private var failed: String?
    @State private var removing: InstalledModel?
    @State private var notice: String?

    private static let sections: [(kind: String, title: String)] =
        [("picture", "Picture models"), ("video", "Video"), ("chat", "Chat models")]

    var body: some View {
        List {
            if loading && models.isEmpty {
                HStack { ProgressView(); Text("Reading your computer…").foregroundStyle(.secondary) }
            }
            if let failed { Text(failed).font(.footnote).foregroundStyle(.secondary) }
            ForEach(Self.sections, id: \.kind) { section in
                let items = models.filter { $0.kind == section.kind }
                if !items.isEmpty {
                    Section(section.title) { ForEach(items) { row($0) } }
                }
            }
        }
        .navigationTitle("Models on your computer")
        .task { await load() }
        .refreshable { await load() }
        .confirmationDialog(removing.map { "Remove \($0.name) from your computer?" } ?? "",
                            isPresented: Binding(get: { removing != nil }, set: { if !$0 { removing = nil } }),
                            titleVisibility: .visible) {
            if let m = removing {
                Button("Remove from computer", role: .destructive) {
                    removing = nil
                    Task {
                        do { notice = try await desktop.removeModel(m.id) }
                        catch { notice = error.localizedDescription }
                        await load()
                    }
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

    @ViewBuilder private func row(_ m: InstalledModel) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(m.name)
                Text(m.inUse ? "In use · \(sizeText(m.sizeGB))" : sizeText(m.sizeGB))
                    .font(.footnote).foregroundStyle(.secondary)
            }
            Spacer()
        }
        .contextMenu {
            if m.inUse {
                Text("In use: switch to another model before removing this one")
            } else {
                Button(role: .destructive) { removing = m } label: {
                    Label("Remove from computer", systemImage: "trash")
                }
            }
        }
        .swipeActions {
            if !m.inUse {
                Button(role: .destructive) { removing = m } label: { Label("Remove", systemImage: "trash") }
            }
        }
    }

    private func sizeText(_ gb: Double) -> String { gb >= 0.1 ? String(format: "%.1f GB", gb) : "small" }

    private func load() async {
        guard desktop.isPaired else { failed = "Your computer isn't connected."; loading = false; return }
        loading = true; failed = nil
        do { models = try await desktop.installedInventory() }
        catch { failed = "Couldn't reach your computer. Pull down to try again." }
        loading = false
    }
}

/// Clear everything inside the Settings tab: Done goes back to the Settings list.
private struct ClearEverythingPage: View {
    let chat: Chat
    @EnvironmentObject private var router: AppRouter
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        ClearEverythingView(chat: chat, onClear: { router.clearedCount += 1 }, onFinished: { dismiss() })
    }
}
