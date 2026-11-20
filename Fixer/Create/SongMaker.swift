import AVFoundation
import SwiftUI

/// 11-20 (the owner: "add a music maker to the maker. Give prompts and it develops a song"). Make's third kind: an
/// idea (and, if wanted, your own lyrics) goes to the computer, its chat model writes the style and lyrics, ACE-Step
/// sings it, and the MP3 comes back here. Songs are kept in the app's own folder until deleted; Share exports one.
struct SongItem: Codable, Identifiable, Hashable {
    var id = UUID()
    var title: String
    var idea: String
    var tags: String
    var lyrics: String
    var seconds: Int
    var made = Date()
    var file: String

    var displayTitle: String { title.isEmpty ? (idea.isEmpty ? "Song" : String(idea.prefix(40))) : title }
}

@MainActor
final class SongStore: ObservableObject {
    static let shared = SongStore()

    @Published private(set) var songs: [SongItem] = []
    @Published var status: String?
    @Published var progress: Double = 0
    @Published var note: String?
    private var job: Task<Void, Never>?
    private var token: UUID?

    var running: Bool { status != nil }

    static var folder: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let dir = base.appendingPathComponent("Songs", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }
    private static var index: URL { folder.appendingPathComponent("songs.json") }

    private init() {
        if let d = try? Data(contentsOf: Self.index), let s = try? JSONDecoder().decode([SongItem].self, from: d) {
            songs = s.filter { FileManager.default.fileExists(atPath: url(of: $0).path) }
        }
    }

    func url(of s: SongItem) -> URL { Self.folder.appendingPathComponent(s.file) }

    private func save() {
        if let d = try? JSONEncoder().encode(songs) { try? d.write(to: Self.index, options: .atomic) }
    }

    func make(idea: String, lyrics: String, instrumental: Bool, seconds: Int) {
        let t = UUID()
        token = t
        note = nil
        status = "Sending to your computer…"
        progress = 0
        job?.cancel()
        job = Task {
            do {
                let (data, meta) = try await Desktop.shared.song(
                    idea: idea, lyrics: lyrics, instrumental: instrumental, seconds: seconds, slot: "make:song",
                    status: { s in Task { @MainActor in if self.token == t { self.status = s } } },
                    progress: { v in Task { @MainActor in if self.token == t { self.progress = v } } },
                    note: { s in Task { @MainActor in if self.token == t { self.note = s } } })
                add(data, meta: meta, idea: idea)
            } catch is CancellationError {
            } catch {
                if token == t, !Task.isCancelled { note = (error as? LocalizedError)?.errorDescription ?? "The song didn't work. Try again." }
            }
            if token == t { status = nil; progress = 0 }
        }
    }

    func cancel() {
        job?.cancel(); job = nil
        token = nil
        status = nil; progress = 0
        Desktop.shared.cancel(slot: "make:song")
    }

    /// A finished song: written to the app's folder and put first in the list.
    func add(_ data: Data, meta: [String: Any], idea: String) {
        let s = SongItem(title: meta["title"] as? String ?? "", idea: idea, tags: meta["tags"] as? String ?? "",
                         lyrics: meta["lyrics"] as? String ?? "", seconds: (meta["seconds"] as? NSNumber)?.intValue ?? 0,
                         file: UUID().uuidString + ".mp3")
        do { try data.write(to: url(of: s), options: .atomic) } catch { note = "The song couldn't be saved."; return }
        songs.insert(s, at: 0)
        save()
    }

    /// Finished while Fixer was closed: filed by JobInbox.
    func adopt(words: String, frames: [[String: Any]]) {
        for f in frames where f["t"] as? String == "audio" {
            guard let b64 = f["v"] as? String, let data = Data(base64Encoded: b64) else { continue }
            add(data, meta: f, idea: words)
        }
    }

    func delete(_ s: SongItem) {
        if SongPlayer.shared.playing == s.id { SongPlayer.shared.stop() }
        try? FileManager.default.removeItem(at: url(of: s))
        songs.removeAll { $0.id == s.id }
        save()
    }
}

@MainActor
final class SongPlayer: NSObject, ObservableObject, AVAudioPlayerDelegate {
    static let shared = SongPlayer()
    @Published private(set) var playing: UUID?
    @Published var position: Double = 0
    private var player: AVAudioPlayer?
    private var timer: Timer?

    func toggle(_ s: SongItem) {
        if playing == s.id { stop(); return }
        stop()
        try? AVAudioSession.sharedInstance().setCategory(.playback)
        try? AVAudioSession.sharedInstance().setActive(true)
        guard let p = try? AVAudioPlayer(contentsOf: SongStore.shared.url(of: s)) else { return }
        p.delegate = self
        p.play()
        player = p
        playing = s.id
        timer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self, let p = self.player, p.duration > 0 else { return }
                self.position = p.currentTime / p.duration
            }
        }
    }

    func stop() {
        player?.stop(); player = nil
        timer?.invalidate(); timer = nil
        playing = nil; position = 0
    }

    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor in self.stop() }
    }
}

/// The Song form and the list of songs, shown in Make when Song is picked.
struct SongMakerView: View {
    @ObservedObject private var store = SongStore.shared
    @ObservedObject private var player = SongPlayer.shared
    @AppStorage("song.idea") private var idea = ""
    @AppStorage("song.lyrics") private var lyrics = ""
    @AppStorage("song.instrumental") private var instrumental = false
    @AppStorage("song.seconds") private var seconds = 120
    @State private var lyricsOpen = false
    @State private var shownLyrics: UUID?
    @FocusState private var focus: Bool

    private let lengths: [(String, Int)] = [("30 s", 30), ("1 min", 60), ("2 min", 120), ("3 min", 180), ("4 min", 240)]

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(spacing: 12) {
                    form
                    if let s = store.status { statusCard(s) }
                    if let n = store.note { noteCard(n) }
                    if store.songs.isEmpty && !store.running {
                        emptyCard
                    } else {
                        ForEach(store.songs) { s in songRow(s) }
                    }
                }
                .padding(.horizontal, 12)
                .padding(.top, 4)
                .padding(.bottom, 16)
                .containerRelativeFrame(.horizontal)
                .clipped()
            }
            .scrollDismissesKeyboard(.interactively)
            .scrollIndicators(.hidden)
            .scrollBounceBehavior(.basedOnSize, axes: .horizontal)
        }
        .safeAreaInset(edge: .bottom) { bottomBar }
    }

    // MARK: form

    private var form: some View {
        VStack(alignment: .leading, spacing: 10) {
            TextField("What's the song? Style, mood, voice, what it's about…", text: $idea, axis: .vertical)
                .lineLimit(2...6)
                .focused($focus)
                .padding(12)
                .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Fx.canvas))
            Toggle(isOn: $instrumental) {
                Label("Instrumental (no singing)", systemImage: "pianokeys")
                    .font(.subheadline.weight(.semibold))
            }
            .tint(Fx.accent)
            .frame(minHeight: 44)
            if !instrumental {
                Button { withAnimation(Motion.pop) { lyricsOpen.toggle() } } label: {
                    HStack {
                        Image(systemName: "text.quote")
                        Text(lyrics.isEmpty ? "Lyrics: written for you" : "Lyrics: yours")
                            .font(.subheadline.weight(.semibold))
                        Spacer()
                        Image(systemName: lyricsOpen ? "chevron.up" : "chevron.down").font(.footnote.weight(.semibold))
                    }
                    .foregroundStyle(Color.primary)
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                if lyricsOpen {
                    TextField("Your own lyrics (optional). Leave empty and they're written for you. Section labels like [Verse] and [Chorus] help.",
                              text: $lyrics, axis: .vertical)
                        .lineLimit(4...14)
                        .focused($focus)
                        .padding(12)
                        .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Fx.canvas))
                }
            }
            HStack(spacing: 6) {
                Text("Length").font(.subheadline.weight(.semibold)).fixedSize()
                    .padding(.trailing, 4)
                ForEach(lengths, id: \.1) { l in
                    Button { seconds = l.1 } label: {
                        Text(l.0)
                            .font(.footnote.weight(.semibold))
                            .lineLimit(1)
                            .foregroundStyle(seconds == l.1 ? Fx.accentSoftText : Color.primary)
                            .frame(maxWidth: .infinity, minHeight: 34)
                            .background(Capsule().fill(seconds == l.1 ? Fx.onTintStrong : Fx.canvas))
                            .overlay(Capsule().strokeBorder(seconds == l.1 ? Fx.accent : .clear, lineWidth: 1))
                            .frame(minHeight: 44)
                            .contentShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    .accessibilityAddTraits(seconds == l.1 ? .isSelected : [])
                }
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 20, style: .continuous).fill(Fx.tile))
    }

    private func statusCard(_ s: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                ProgressView()
                Text(s).font(.subheadline.weight(.semibold))
                Spacer()
            }
            if store.progress > 0 { ProgressView(value: store.progress).tint(Fx.accent) }
        }
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Fx.tile))
    }

    private func noteCard(_ n: String) -> some View {
        HStack(alignment: .top) {
            Text(n).font(.footnote).foregroundStyle(Fx.secondaryText)
            Spacer()
            Button { store.note = nil } label: { Image(systemName: "xmark").font(.footnote.weight(.bold)) }
                .buttonStyle(.plain)
                .frame(minWidth: 44, minHeight: 44)
                .accessibilityLabel("Dismiss")
        }
        .padding(.leading, 14)
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Fx.tile))
    }

    private var emptyCard: some View {
        VStack(spacing: 8) {
            Image(systemName: "music.note").font(.system(size: 34, weight: .light)).foregroundStyle(Fx.secondaryText)
            Text("Your songs show here").font(.subheadline).foregroundStyle(Fx.secondaryText)
        }
        .frame(maxWidth: .infinity, minHeight: 140)
        .background(RoundedRectangle(cornerRadius: 20, style: .continuous).fill(Fx.tile))
    }

    // MARK: songs

    private func songRow(_ s: SongItem) -> some View {
        let on = player.playing == s.id
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                Button { player.toggle(s) } label: {
                    Image(systemName: on ? "stop.fill" : "play.fill")
                        .font(.system(size: 18, weight: .semibold))
                        .foregroundStyle(Fx.onSolid)
                        .frame(width: 44, height: 44)
                        .background(Circle().fill(Fx.solid))
                }
                .buttonStyle(.plain)
                .accessibilityLabel(on ? "Stop \(s.displayTitle)" : "Play \(s.displayTitle)")
                VStack(alignment: .leading, spacing: 2) {
                    Text(s.displayTitle).font(.subheadline.weight(.semibold)).lineLimit(1)
                    Text("\(Self.length(s.seconds)) · \(s.tags)").font(.caption).foregroundStyle(Fx.secondaryText).lineLimit(1)
                }
                Spacer(minLength: 0)
                ShareLink(item: SongStore.shared.url(of: s)) {
                    Image(systemName: "square.and.arrow.up").frame(width: 44, height: 44)
                }
                .accessibilityLabel("Share \(s.displayTitle)")
                Menu {
                    if !s.lyrics.isEmpty, s.lyrics != "[Instrumental]" {
                        Button(shownLyrics == s.id ? "Hide lyrics" : "Show lyrics", systemImage: "text.quote") {
                            shownLyrics = shownLyrics == s.id ? nil : s.id
                        }
                    }
                    Button("Use this idea again", systemImage: "arrow.counterclockwise") {
                        idea = s.idea
                    }
                    Button("Delete", systemImage: "trash", role: .destructive) { store.delete(s) }
                } label: {
                    Image(systemName: "ellipsis").frame(width: 44, height: 44).contentShape(Rectangle())
                }
                .accessibilityLabel("More for \(s.displayTitle)")
            }
            .foregroundStyle(Color.primary)
            if on { ProgressView(value: player.position).tint(Fx.accent) }
            if shownLyrics == s.id {
                Text(s.lyrics).font(.footnote).foregroundStyle(Fx.secondaryText).textSelection(.enabled)
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Fx.tile))
    }

    private static func length(_ s: Int) -> String { s >= 60 ? "\(s / 60):" + String(format: "%02d", s % 60) : "\(s) s" }

    // MARK: button

    private var bottomBar: some View {
        let stop = store.running
        let ready = !idea.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            || (!instrumental && !lyrics.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
        return Button {
            if stop { store.cancel(); return }
            focus = false
            store.make(idea: idea.trimmingCharacters(in: .whitespacesAndNewlines),
                       lyrics: instrumental ? "" : lyrics.trimmingCharacters(in: .whitespacesAndNewlines),
                       instrumental: instrumental, seconds: seconds)
        } label: {
            HStack(spacing: 8) {
                Image(systemName: stop ? "stop.fill" : "music.note").font(.system(size: stop ? 12 : 16, weight: .semibold))
                Text(stop ? "Stop" : "Make song").font(.body.weight(.semibold))
            }
            .foregroundStyle(stop ? Color.primary : Fx.onSolid)
            .frame(maxWidth: .infinity, minHeight: 50)
            .background(Capsule().fill(stop ? Fx.solidSoft : Fx.solid))
            .opacity(stop || ready ? 1 : 0.4)
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .disabled(!stop && !ready)
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(.bar)
    }
}
