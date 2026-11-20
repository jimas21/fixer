import SwiftUI

/// Clear everything, in three stages: what will be erased (counts, on this iPhone and on the computer, and what
/// stays), then each part ticked off as it goes, then a check that nothing is left and a summary.
struct ClearEverythingView: View {
    @ObservedObject var chat: Chat
    var onClear: () -> Void                    // the chat screen drops its draft, attachment and viewers
    var onFinished: () -> Void                 // closes History
    var startNow = false                       // already confirmed by the caller, go straight to erasing
    var eraseSongs = false                     // 11-20: songs stay unless this erase was asked to take them too
    @ObservedObject private var desktop = Desktop.shared
    @Environment(\.dismiss) private var dismiss

    @State private var inventory = History.inventory()
    @State private var created = CreateStore.shared.results.count
    @State private var library = LibraryVault.inventory()
    @State private var stage = Stage.review
    @State private var steps: [Step] = []
    @State private var confirm = false
    @State private var summary = ""
    @State private var songsToo: Bool?
    private var takeSongs: Bool { songsToo ?? eraseSongs }
    private var songCount: Int { SongStore.shared.songs.count }

    enum Stage { case review, erasing, finished }
    enum Mark { case waiting, working, done, warning, failed }
    struct Step: Identifiable {
        let id: String
        let title: String
        var detail: String?
        var mark = Mark.waiting
    }

    var body: some View {
        List {
            if stage == .review && !startNow { review } else { progress }
        }
        .task { if startNow, stage == .review { await run() } }
        .navigationTitle(TidyText.eraseTitle)
        .navigationBarTitleDisplayMode(.inline)
        .navigationBarBackButtonHidden(stage == .erasing)
        .interactiveDismissDisabled(stage == .erasing)
        .confirmationDialog("Erase everything now?", isPresented: $confirm, titleVisibility: .visible) {
            Button("Erase everything", role: .destructive) { Task { await run() } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This can't be undone.")
        }
    }

    // MARK: before

    @ViewBuilder private var review: some View {
        Section {
            count("Conversations", inventory.conversations, "bubble.left.and.bubble.right")
            count("Photos and pictures", inventory.pictures, "photo")
            count("Videos", inventory.videos, "film")
            count("Pictures on the Make screen", created, "paintbrush.pointed")
            if songCount > 0 {
                Toggle(isOn: Binding(get: { takeSongs }, set: { songsToo = $0 })) {
                    Label("Erase my \(plural(songCount, "song", "songs")) too", systemImage: "music.note")
                }
            }
            if library.unprotected > 0 {
                count("Library items", library.unprotected, "photo.stack")
            }
            Label("Temporary files and the app-switcher snapshot", systemImage: "doc.badge.clock")
            Label("The on-phone AI's memory", systemImage: "brain")
        } header: {
            Text("On this iPhone")
        } footer: {
            Text("Photos and videos you added count as well as ones Fixer made. \(sizeText(inventory.bytes + library.bytes)) in all, Library included.")
        }

        if desktop.isPaired {
            Section {
                Label("The chat model's memory of your conversations", systemImage: "text.bubble")
                Label("The picture and video engine, with any photo or prompt it's holding", systemImage: "cpu")
                Label("Its private temporary folder", systemImage: "folder")
            } header: {
                Text("On your computer")
            } footer: {
                Text("Your computer keeps no history: everything there lives only in memory or in a private folder "
                     + "that's emptied. If it's off or away now, it erases the moment it's reached, before anything else.")
            }
        }

        Section("Stays") {
            Label("Settings and your custom instructions", systemImage: "gearshape")
            if songCount > 0 && !takeSongs {
                Label("Your \(plural(songCount, "song", "songs")) and albums", systemImage: "music.note")
            }
            if library.protected > 0 {
                Label("\(plural(library.protected, "protected Library item", "protected Library items"))", systemImage: "lock.fill")
            }
            Label("Downloaded models", systemImage: "square.and.arrow.down")
            Label("Photos and videos you saved to your Photos library", systemImage: "photo.on.rectangle")
        }

        Section {
            Button(role: .destructive) { confirm = true } label: {
                Label("Erase everything", systemImage: "trash").frame(maxWidth: .infinity)
            }
        }
    }

    private func count(_ title: String, _ n: Int, _ icon: String) -> some View {
        LabeledContent { Text("\(n)").monospacedDigit() } label: { Label(title, systemImage: icon) }
    }

    // MARK: during and after

    @ViewBuilder private var progress: some View {
        Section(stage == .finished ? "Done" : "Erasing…") {
            ForEach(steps) { s in
                HStack(alignment: .top, spacing: 12) {
                    icon(s.mark).frame(width: 22)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(s.title)
                        if let d = s.detail { Text(d).font(.footnote).foregroundStyle(.secondary) }
                    }
                }
            }
        }
        if stage == .finished {
            Section {
                Text(summary)
                Button("Done") { onFinished() }.frame(maxWidth: .infinity)
            }
        }
    }

    @ViewBuilder private func icon(_ m: Mark) -> some View {
        switch m {
        case .waiting: Image(systemName: "circle").foregroundStyle(.tertiary)
        case .working: ProgressView()
        case .done: Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
        case .warning: Image(systemName: "exclamationmark.circle.fill").foregroundStyle(.orange)
        case .failed: Image(systemName: "xmark.circle.fill").foregroundStyle(.red)
        }
    }

    private func set(_ id: String, _ mark: Mark, _ detail: String? = nil) {
        guard let i = steps.firstIndex(where: { $0.id == id }) else { return }
        steps[i].mark = mark
        if let detail { steps[i].detail = detail }
    }

    /// Runs one step with a visible tick (at least a third of a second, so the list reads as it goes).
    private func step(_ id: String, _ work: () -> String?) async {
        set(id, .working)
        let started = Date()
        let detail = work()
        let left = 0.35 - Date().timeIntervalSince(started)
        if left > 0 { try? await Task.sleep(nanoseconds: UInt64(left * 1_000_000_000)) }
        set(id, .done, detail)
    }

    private func plural(_ n: Int, _ one: String, _ many: String) -> String { "\(n) \(n == 1 ? one : many)" }

    private func sizeText(_ bytes: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }

    private func run() async {
        let before = inventory
        let createdBefore = created
        let libraryBefore = LibraryVault.inventory()
        steps = [
            Step(id: "stop", title: "Stop anything running"),
            Step(id: "library", title: "Library"),
            Step(id: "videos", title: "Videos"),
            Step(id: "pictures", title: "Photos and pictures"),
            Step(id: "conversations", title: "Conversations"),
            Step(id: "create", title: "Pictures on the Make screen"),
            Step(id: "songs", title: "Songs"),
            Step(id: "scratch", title: "Temporary files and app-switcher snapshot"),
            Step(id: "brain", title: "On-phone AI memory"),
            Step(id: "check", title: "Check: nothing left on this iPhone"),
        ]
        if desktop.isPaired {
            steps.append(Step(id: "pc", title: "Your computer"))
            Desktop.shared.forgetOwed = true           // owed first, before anything is deleted: survives a crash
        }
        stage = .erasing
        onClear()

        await step("stop") {
            chat.wipeSession()
            CreateStore.shared.clear()      // bumps the epoch and cancels Make work before the Library is touched
            return nil
        }
        // The Library: wait for saves still being written, erase every item that isn't protected, then lock it.
        set("library", .working)
        let erasedFromLibrary = await LibraryStore.shared.eraseUnprotected()
        LibraryStore.shared.lock()
        set("library", .done, libraryBefore.protected == 0
            ? plural(erasedFromLibrary, "item", "items") + " erased"
            : plural(erasedFromLibrary, "item", "items") + " erased, "
              + plural(libraryBefore.protected, "protected item", "protected items") + " kept")
        await step("videos") { plural(History.deleteFiles(History.videoTypes), "video", "videos") + " deleted" }
        await step("pictures") { plural(History.deleteFiles(History.pictureTypes), "picture", "pictures") + " deleted" }
        await step("conversations") {
            History.deleteAll()
            return plural(before.conversations, "conversation", "conversations") + " deleted"
        }
        await step("create") {
            CreateStore.shared.clear()
            return plural(createdBefore, "picture", "pictures") + " cleared"
        }
        let songsBefore = songCount
        await step("songs") {
            if takeSongs { return plural(SongStore.shared.deleteAll(), "song", "songs") + " deleted" }
            SongStore.shared.cancel()
            return songsBefore == 0 ? "None" : plural(songsBefore, "song", "songs") + " kept, as you chose"
        }
        await step("scratch") {
            let n = History.scratchLeft
            History.wipeScratch()
            return n == 0 ? "Nothing was there" : plural(n, "file", "files") + " deleted"
        }
        await step("brain") { Brain.shared.forget(); return nil }

        set("check", .working)
        try? await Task.sleep(nanoseconds: 350_000_000)
        await LibraryStore.shared.flush()
        let after = History.inventory()
        let scratch = History.scratchLeft
        let libraryAfter = LibraryVault.inventory()
        let phoneClean = after.isEmpty && scratch == 0 && libraryAfter.unprotected == 0
        set("check", phoneClean ? .done : .failed,
            phoneClean ? (libraryAfter.protected == 0
                          ? "No conversations, pictures, videos or temporary files remain"
                          : "Nothing remains except \(plural(libraryAfter.protected, "protected Library item", "protected Library items"))")
                       : "\(plural(after.pictures + after.videos + after.other + scratch + libraryAfter.unprotected, "file", "files")) couldn't be deleted. Try again.")

        var pcLine = ""
        if desktop.isPaired {
            set("pc", .working, "Asking it to erase…")
            if let report = await Desktop.shared.forget() {
                let erased = report.erased.joined(separator: "; ")
                if report.left.isEmpty {
                    set("pc", .done, "Erased: \(erased). It checked: nothing left.")
                    pcLine = " Your computer erased its side and confirmed nothing is left."
                } else {
                    set("pc", .warning, "Erased: \(erased). Still there: \(report.left.joined(separator: "; ")).")
                    pcLine = " Your computer erased its side but reported something it couldn't remove."
                }
            } else {
                set("pc", .warning, "Off or away right now. It erases the moment it's reached, before anything else, "
                    + "and on its own after 20 idle minutes.")
                pcLine = " Your computer will erase its side the moment it's reached."
            }
        }

        let total = before.pictures + before.videos
        var line: String
        if phoneClean {
            let gone = plural(before.conversations, "conversation", "conversations")
            let media = plural(total + erasedFromLibrary, "photo or video", "photos and videos")
            line = "Erased \(gone) and \(media) (\(sizeText(before.bytes + libraryBefore.bytes))) from this iPhone."
        } else {
            line = "Some files on this iPhone couldn't be deleted. Try again."
        }
        if libraryAfter.protected > 0 {
            line += " \(plural(libraryAfter.protected, "protected Library item", "protected Library items")) stayed, as you chose."
        }
        summary = line + pcLine
        stage = .finished
    }
}
