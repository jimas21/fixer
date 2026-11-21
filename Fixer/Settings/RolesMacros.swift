import SwiftUI

/// Settings → Add-on roles: which installed LoRA each named role uses (the Realism look, the explicit add-on…),
/// its strength and whether it is on. Saved on the computer; macros are passed through untouched.
struct RolesMacros: View {
    @ObservedObject private var desktop = Desktop.shared
    @State private var roles: [Desktop.Role] = []
    @State private var macros: [Desktop.Macro] = []
    @State private var library: [LoraEntry] = []
    @State private var loading = true
    @State private var saving = false
    @State private var failed: String?
    @State private var notice: String?

    /// The built-in roles, in the order shown, with their labels.
    private static let known: [(name: String, label: String)] = [
        ("realism", "Realism look"),
        ("skin-detail", "Skin detail"),
        ("undress-helper", "Undress helper"),
        ("qwen-explicit-addon", "Qwen explicit add-on"),
    ]

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
                ForEach(Self.known, id: \.name) { item in
                    if let i = roles.firstIndex(where: { $0.name == item.name }) {
                        roleSection(item.label, index: i)
                    }
                }
                Section {
                    Button(saving ? "Saving…" : "Save") { Task { await save() } }
                        .disabled(saving)
                    if let notice { Text(notice).font(.footnote).foregroundStyle(.secondary) }
                }
            }
        }
        .navigationTitle("Add-on roles")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
    }

    /// Installed LoRAs this role may use. The current choice is always kept so the picker never loses it.
    private func choices(for role: Desktop.Role) -> [LoraEntry] {
        var list = library
        if role.name == "qwen-explicit-addon" { list = list.filter { $0.family.hasPrefix("qwen") } }
        if !role.file.isEmpty, !list.contains(where: { $0.file == role.file }) {
            list.append(library.first { $0.file == role.file } ?? LoraEntry(file: role.file))
        }
        return list
    }

    @ViewBuilder private func roleSection(_ label: String, index i: Int) -> some View {
        Section(label) {
            Picker("Add-on", selection: $roles[i].file) {
                Text("None").tag("")
                ForEach(choices(for: roles[i])) { l in
                    Text("\(l.name) · \(LoraEntry.familyName(l.family))").tag(l.file)
                }
            }
            VStack(alignment: .leading) {
                LabeledContent("Strength", value: String(format: "%.2f", roles[i].strength))
                Slider(value: $roles[i].strength, in: 0...2, step: 0.05)
            }
            Toggle("Enabled", isOn: $roles[i].enabled)
        }
    }

    private func load() async {
        guard desktop.isPaired else { failed = "Your computer isn't connected."; loading = false; return }
        loading = true; failed = nil
        do {
            let r = try await desktop.macrosGet()
            roles = r.roles; macros = r.macros; library = r.library
        } catch {
            failed = "Couldn't reach your computer."
        }
        loading = false
    }

    private func save() async {
        saving = true; notice = nil
        do {
            let r = try await desktop.macrosSet(roles: roles, macros: macros)
            roles = r.roles; macros = r.macros; library = r.library
            notice = "Saved."
        } catch {
            notice = "Couldn't save. Try again."
        }
        saving = false
    }
}
