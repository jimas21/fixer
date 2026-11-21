import AVFoundation
import Photos
import SwiftUI
import UIKit

/// Everything Fixer made, kept sealed on this iPhone. Opens with Face ID or the passcode, once; locks again when the
/// app leaves the screen.
struct LibraryScreen: View {
    @ObservedObject var chat: Chat
    @ObservedObject private var store = LibraryStore.shared
    @EnvironmentObject private var router: AppRouter
    @Environment(\.scenePhase) private var phase

    enum Filter: String, CaseIterable, Identifiable {
        case all = "All", pictures = "Pictures", videos = "Videos", protected = "Protected"
        var id: String { rawValue }
    }

    @State private var filter = Filter.all
    @State private var viewing: LibraryEntry?

    private let columns = [GridItem(.adaptive(minimum: 104), spacing: 6)]

    var body: some View {
        NavigationStack {
            Group {
                switch store.state {
                case .unlocked: grid
                case .unavailable(let why): notice(why, button: nil)
                case .locked, .unlocking: locked
                }
            }
            .tabBarClearance()
            .navigationTitle("Library")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    TidyMenu(chat: chat, onClear: { router.clearedCount += 1 }) { TidyCircleLabel() }
                }
            }
            .task(id: router.tab) { if router.tab == .library { await store.unlock() } }
            .onChange(of: phase) { _, now in
                if now == .active, router.tab == .library { Task { await store.unlock(automatic: true) } }
            }
            .onChange(of: store.state) { _, now in if now != .unlocked { viewing = nil } }
            .fullScreenCover(item: $viewing) { entry in
                LibraryMediaViewer(start: entry.id, filter: filter, chat: chat, router: router)
            }
        }
    }

    // MARK: locked

    private var locked: some View {
        let n = store.counts
        return notice(n.items == 0 ? "Everything Fixer makes is kept here, locked. Nothing is saved yet."
                                   : "\(n.items) \(n.items == 1 ? "item" : "items") kept locked. Unlock with Face ID or your passcode to see them.",
                      button: store.state == .unlocking ? nil : "Unlock Library")
    }

    private func notice(_ text: String, button: String?) -> some View {
        VStack(spacing: 16) {
            Image(systemName: "lock.fill").font(.system(size: 40)).foregroundStyle(.secondary)
            Text(text).multilineTextAlignment(.center).foregroundStyle(.secondary)
            if let message = store.message { Text(message).font(.footnote).foregroundStyle(.red) }
            if store.state == .unlocking { ProgressView() }
            if let button {
                Button(button) { Task { await store.unlock() } }.buttonStyle(.borderedProminent)
            }
        }
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: open

    static func visible(_ entries: [LibraryEntry], _ filter: Filter) -> [LibraryEntry] {
        entries.filter { e in
            switch filter {
            case .all: true
            case .pictures: e.meta.kind == .picture
            case .videos: e.meta.kind == .video
            case .protected: e.isProtected
            }
        }
    }

    private var shown: [LibraryEntry] { Self.visible(store.entries, filter) }

    private var grid: some View {
        let kept = store.entries.filter(\.isProtected).count
        let erased = store.entries.count - kept
        return ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                Picker("Show", selection: $filter) {
                    ForEach(Filter.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                Text("\(kept) protected · \(erased) will be erased by Erase everything")
                    .font(.footnote).foregroundStyle(.secondary)
                if store.entries.isEmpty {
                    Text("Nothing here yet. Pictures and videos you make show up here.")
                        .foregroundStyle(.secondary).padding(.top, 40).frame(maxWidth: .infinity)
                } else if shown.isEmpty {
                    Text("Nothing in this view.").foregroundStyle(.secondary).padding(.top, 40).frame(maxWidth: .infinity)
                }
                LazyVGrid(columns: columns, spacing: 6) {
                    ForEach(shown) { e in tile(e) }
                }
            }
            .padding(.horizontal, 12).padding(.vertical, 8)
        }
    }

    private func tile(_ e: LibraryEntry) -> some View {
        Button { viewing = e } label: {
            Color(.secondarySystemBackground)
                .aspectRatio(1, contentMode: .fit)
                .overlay {
                    if let thumb = store.thumbnail(for: e) {
                        Image(uiImage: thumb).resizable().scaledToFill()
                    } else {
                        Image(systemName: e.meta.kind == .video ? "film" : "photo").foregroundStyle(.secondary)
                    }
                }
                .clipShape(RoundedRectangle(cornerRadius: 8))
                .overlay(alignment: .bottomLeading) { badge(e) }
                .overlay(alignment: .topTrailing) {
                    if e.meta.kind == .video {
                        Image(systemName: "play.fill").font(.caption2).foregroundStyle(.white)
                            .padding(5).background(.black.opacity(0.5), in: Circle()).padding(4)
                    }
                }
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(e.meta.kind == .video ? "Video" : "Picture"), \(e.isProtected ? "protected" : "will be erased by Erase everything")")
    }

    private func badge(_ e: LibraryEntry) -> some View {
        Label(e.isProtected ? "Protected" : "Erases", systemImage: e.isProtected ? "lock.fill" : "trash")
            .font(.caption2.weight(.semibold))
            .foregroundStyle(.white)
            .padding(.horizontal, 6).padding(.vertical, 3)
            .background((e.isProtected ? Color.green : Color.gray).opacity(0.85), in: Capsule())
            .padding(4)
    }
}

/// The Library's side of the shared viewer: the items in the filter being shown, paged. Temporary: it is closed
/// whenever the Library locks, and a video's decrypted copy is removed with it. Delete and Protect work here;
/// "To chat" and "Make another" hand the item to the other tabs.
private struct LibraryMediaViewer: View {
    let filter: LibraryScreen.Filter
    let chat: Chat
    let router: AppRouter
    @ObservedObject private var store = LibraryStore.shared
    @State private var current: UUID

    init(start: UUID, filter: LibraryScreen.Filter, chat: Chat, router: AppRouter) {
        self.filter = filter
        self.chat = chat
        self.router = router
        _current = State(initialValue: start)
    }

    private var items: [MediaItem] {
        // 11-21: entries are newest-first; reverse so the viewer runs oldest -> newest with the newest on the right.
        LibraryScreen.visible(store.entries, filter).reversed().map {
            MediaItem(id: $0.id, kind: $0.meta.kind == .video ? .video : .picture, source: .library($0.id),
                      libraryID: $0.id, recipeID: $0.meta.recipeID, enhanced: $0.meta.enhanced)
        }
    }

    private var actions: MediaActions {
        var a = MediaActions()
        a.closesAfterAction = true
        a.delete = { item in store.delete(item.id) }
        a.toChat = { [chat, router] item in
            Task {
                guard let ui = await LibraryStore.shared.image(item.id), let cg = ui.cgImage else { return }
                chat.commit(cg, caption: "From your Library.", libraryID: item.id)
                router.tab = .chat
            }
        }
        a.makeAnother = { [router] item in
            guard let entry = LibraryStore.shared.entries.first(where: { $0.id == item.id }) else { return }
            router.makeAgain = entry.meta
            router.tab = .make
        }
        return a
    }

    var body: some View {
        MediaViewer(items: items, current: $current, actions: actions)
    }
}
