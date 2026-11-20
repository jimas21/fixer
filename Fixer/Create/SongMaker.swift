import AVFoundation
import MediaPlayer
import PhotosUI
import SwiftUI
import UniformTypeIdentifiers

/// 11-20 (the owner: "add a music maker to the maker. Give prompts and it develops a song", then "more options:
/// genre selections, dynamic song length, full albums"). Make's third kind. An idea, a sound (picked genres, moods,
/// voices, instruments, eras, tempo; artists it should sound like; a style line), lyrics (written for you, your
/// own, or none) and a length (Auto or chosen) go to the computer; its chat model writes the song and ACE-Step sings
/// it. An album is a theme and a number of tracks: a tracklist with one shared sound and a cover, each track sent as
/// it is done. A recording (a file, one of your songs, or a link to your own on YouTube / SoundCloud) can be the
/// starting point: ACE-Step's cover mode re-performs it in the new sound. Songs stay in the app's own folder.

// MARK: - Model

struct ArtistPick: Codable, Hashable, Identifiable {
    var id: String
    var name: String
    var genres: [String] = []
    var detail: String?
}

struct SongItem: Codable, Identifiable, Hashable {
    var id = UUID()
    var title: String
    var idea: String
    var tags: String
    var lyrics: String
    var seconds: Int
    var made = Date()
    var file: String
    var album: String?
    var albumTitle: String?
    var track: Int?
    var bpm: Int?
    var key: String?
    var language: String?
    var seed: Int?
    var art: String?

    /// The key of its artwork file (cover-<key>.png): an album's id for album tracks, else its own.
    var artKey: String { album ?? art ?? id.uuidString }

    var displayTitle: String { title.isEmpty ? (idea.isEmpty ? "Song" : String(idea.prefix(40))) : title }
    var instrumental: Bool { lyrics.isEmpty || lyrics == "[Instrumental]" }
}

/// What the song form holds, in memory only (see fresh()).
struct SongDraft: Codable, Equatable {
    enum Lyrics: String, Codable, CaseIterable { case written, mine, none }
    var album = false
    var idea = ""
    var lyrics = ""
    var lyricMode = Lyrics.written
    var picks: [String: [String]] = [:]
    var style = ""
    var language = "en"
    var auto = true
    var seconds = 120
    var tracks = 6
    var artists: [ArtistPick] = []
    var link = ""
    var artwork = true                     // 11-20: paint artwork for a song as it is made (albums always get a cover)
    var explicitLyrics = false             // 11-20: swearing allowed (and adult themes while 18+ is on); saved
    var keep = "balanced"                  // 11-20: how close a song stays to its recording: loose, balanced, close

    /// 11-20 (the owner: "if the app fully closes it should clear all fields but still keep the produced material"):
    /// the form lives in memory only. It survives switching apps; a full close starts it empty. The one saved choice
    /// is "Make artwork" (a preference, not a prompt). Anything an older build saved is dropped.
    static func fresh() -> SongDraft {
        UserDefaults.standard.removeObject(forKey: "song.draft")
        var d = SongDraft()
        d.artwork = UserDefaults.standard.object(forKey: "song.artwork") as? Bool ?? true
        d.explicitLyrics = UserDefaults.standard.bool(forKey: "song.explicitLyrics")
        return d
    }

    /// The picked sound as style tags, then the typed style line.
    var tags: String {
        let picked = SongPicks.groups.flatMap { g in (picks[g.id] ?? []).map { $0.lowercased() } }
        return (picked + [style.trimmingCharacters(in: .whitespacesAndNewlines)]).filter { !$0.isEmpty }
            .joined(separator: ", ")
    }
}

/// A recording to start from (cover mode).
struct SongReference: Equatable {
    var name: String
    var data: Data
    var ext: String
    var seconds: Int?
}

/// The sound pick groups (static on the phone so the form opens instantly).
enum SongPicks {
    struct Group: Identifiable, Hashable { let id: String; let title: String; let options: [String] }
    static let groups: [Group] = [
        Group(id: "genre", title: "Genre", options: [
            "Pop", "Rock", "Indie", "Alternative", "Hip hop", "Trap", "R&B", "Soul", "Funk", "Jazz", "Blues",
            "Country", "Folk", "Acoustic", "Singer-songwriter", "Electronic", "House", "Techno", "Drum and bass",
            "Dubstep", "EDM", "Synthwave", "Lo-fi", "Ambient", "Classical", "Cinematic", "Orchestral", "Metal",
            "Punk", "Grunge", "Emo", "Reggae", "Ska", "Latin", "Reggaeton", "Salsa", "Bossa nova", "K-pop", "J-pop",
            "Afrobeats", "Gospel", "Disco", "Musical theatre", "Chiptune", "Bluegrass", "Shoegaze", "Hyperpop",
        ]),
        Group(id: "mood", title: "Mood", options: [
            "Happy", "Upbeat", "Energetic", "Chill", "Dreamy", "Romantic", "Sensual", "Sad", "Melancholic", "Dark",
            "Angry", "Epic", "Hopeful", "Nostalgic", "Playful", "Mysterious", "Triumphant", "Peaceful", "Haunting",
        ]),
        Group(id: "voice", title: "Voice", options: [
            "Female vocal", "Male vocal", "Duet", "Choir", "Rap", "Soft breathy vocal", "Powerful belting",
            "Raspy vocal", "Falsetto", "Deep baritone", "Auto-tuned vocal", "Spoken word", "Harmonies", "Whispered",
        ]),
        Group(id: "instruments", title: "Instruments", options: [
            "Acoustic guitar", "Electric guitar", "Piano", "Synth", "808 bass", "Bass guitar", "Strings", "Brass",
            "Saxophone", "Violin", "Cello", "Live drums", "Drum machine", "Banjo", "Organ", "Harp", "Flute",
            "Ukulele", "Steel guitar", "Choir pads",
        ]),
        Group(id: "era", title: "Era", options: ["50s", "60s", "70s", "80s", "90s", "2000s", "2010s", "Modern"]),
        Group(id: "tempo", title: "Tempo", options: ["Slow", "Mid-tempo", "Fast", "Half-time", "Building"]),
        Group(id: "production", title: "Production", options: [
            "Polished", "Lo-fi", "Live band", "Stripped back", "Wall of sound", "Vinyl crackle", "Reverb-heavy",
            "Radio-ready", "Bedroom pop", "Big room",
        ]),
    ]
    static let languages: [(String, String)] = [
        ("en", "English"), ("es", "Spanish"), ("fr", "French"), ("de", "German"), ("it", "Italian"),
        ("pt", "Portuguese"), ("nl", "Dutch"), ("sv", "Swedish"), ("pl", "Polish"), ("ru", "Russian"),
        ("uk", "Ukrainian"), ("tr", "Turkish"), ("ar", "Arabic"), ("hi", "Hindi"), ("ja", "Japanese"),
        ("ko", "Korean"), ("zh", "Chinese"), ("yue", "Cantonese"), ("vi", "Vietnamese"), ("th", "Thai"),
        ("id", "Indonesian"), ("tl", "Tagalog"),
    ]
}

/// Artist look-up on MusicBrainz (free, no account): name, a line about who they are, and their top genres.
enum MusicBrainz {
    static func search(_ query: String) async throws -> [ArtistPick] {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard q.count >= 2 else { return [] }
        var c = URLComponents(string: "https://musicbrainz.org/ws/2/artist")!
        c.queryItems = [URLQueryItem(name: "query", value: q), URLQueryItem(name: "fmt", value: "json"),
                        URLQueryItem(name: "limit", value: "8")]
        guard let url = c.url else { return [] }
        var r = URLRequest(url: url)
        r.timeoutInterval = 8
        r.setValue("Fixer/1.0 (private iPhone app)", forHTTPHeaderField: "User-Agent")
        let (data, _) = try await URLSession.shared.data(for: r)
        guard let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let list = obj["artists"] as? [[String: Any]] else { return [] }
        return list.compactMap { a in
            guard let id = a["id"] as? String, let name = a["name"] as? String else { return nil }
            let tags = ((a["tags"] as? [[String: Any]]) ?? [])
                .sorted { ($0["count"] as? Int ?? 0) > ($1["count"] as? Int ?? 0) }
                .compactMap { $0["name"] as? String }
            let bits = [a["disambiguation"] as? String, a["country"] as? String, a["type"] as? String]
                .compactMap { $0 }.filter { !$0.isEmpty }
            return ArtistPick(id: id, name: name, genres: Array(tags.prefix(5)),
                              detail: bits.isEmpty ? nil : bits.joined(separator: " · "))
        }
    }
}

// MARK: - Store

@MainActor
final class SongStore: ObservableObject {
    static let shared = SongStore()

    @Published private(set) var songs: [SongItem] = []
    @Published var status: String?
    @Published var progress: Double = 0
    @Published var note: String?
    @Published var albumPlan: (title: String, titles: [String])?
    @Published var draft = SongDraft.fresh() {
        didSet {
            if draft.artwork != oldValue.artwork { UserDefaults.standard.set(draft.artwork, forKey: "song.artwork") }
            if draft.explicitLyrics != oldValue.explicitLyrics {
                UserDefaults.standard.set(draft.explicitLyrics, forKey: "song.explicitLyrics")
            }
        }
    }
    @Published var writing = false
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
            songs = s.filter { FileManager.default.fileExists(atPath: Self.folder.appendingPathComponent($0.file).path) }
        }
    }

    func url(of s: SongItem) -> URL { Self.folder.appendingPathComponent(s.file) }
    func coverURL(_ key: String) -> URL { Self.folder.appendingPathComponent("cover-\(key).png") }
    func cover(_ key: String) -> UIImage? { artVersion >= 0 ? UIImage(contentsOfFile: coverURL(key).path) : nil }
    /// Bumped whenever a cover file changes, so the list redraws its pictures.
    @Published private(set) var artVersion = 0
    /// Artwork being painted on the computer, by key.
    @Published private(set) var painting: Set<String> = []

    func setArt(_ key: String, png: Data) {
        try? png.write(to: coverURL(key), options: .atomic)
        artVersion += 1
    }

    /// A photo from the library as the artwork: square-cropped to 1024.
    func setArt(_ key: String, image: UIImage) {
        let side = min(image.size.width, image.size.height)
        guard side > 0 else { return }
        let target = CGSize(width: 1024, height: 1024)
        let out = UIGraphicsImageRenderer(size: target).image { _ in
            let scale = 1024 / side
            let w = image.size.width * scale, h = image.size.height * scale
            image.draw(in: CGRect(x: (1024 - w) / 2, y: (1024 - h) / 2, width: w, height: h))
        }
        if let d = out.jpegData(compressionQuality: 0.9) { setArt(key, png: d) }
    }

    func removeArt(_ key: String) {
        try? FileManager.default.removeItem(at: coverURL(key))
        artVersion += 1
    }

    /// Paints new artwork on the computer for a song or an album (its own job: it never stops a song being made).
    func paintArt(key: String, title: String, tags: String, lyrics: String, idea: String, wish: String) {
        guard !painting.contains(key) else { return }
        painting.insert(key)
        let p: [String: Any] = ["art_id": key, "title": title, "tags": tags, "lyrics": String(lyrics.prefix(1500)),
                                "idea": idea, "prompt": wish, "label": "Artwork: " + (title.isEmpty ? "song" : title)]
        Task {
            do {
                try await Desktop.shared.music("artwork", p, slot: "make:art:" + key) { [weak self] f in
                    guard let self else { return }
                    if f["t"] as? String == "cover" { self.handle(f, idea: idea) }
                }
            } catch {
                note = (error as? LocalizedError)?.errorDescription ?? "The artwork couldn't be painted."
            }
            painting.remove(key)
        }
    }

    private func save() {
        if let d = try? JSONEncoder().encode(songs) { try? d.write(to: Self.index, options: .atomic) }
    }

    /// Songs and albums for the list, newest first; an album's tracks in running order.
    enum Entry: Identifiable {
        case song(SongItem)
        case album(id: String, title: String, tracks: [SongItem])
        var id: String {
            switch self {
            case .song(let s): return s.id.uuidString
            case .album(let id, _, _): return "album:" + id
            }
        }
    }
    var entries: [Entry] {
        var out: [Entry] = []
        var seen = Set<String>()
        for s in songs {
            if let a = s.album {
                guard !seen.contains(a) else { continue }
                seen.insert(a)
                let tracks = songs.filter { $0.album == a }.sorted { ($0.track ?? 0) < ($1.track ?? 0) }
                out.append(.album(id: a, title: s.albumTitle ?? "Album", tracks: tracks))
            } else {
                out.append(.song(s))
            }
        }
        return out
    }

    // MARK: making

    private func payload(_ d: SongDraft, ref: SongReference?) -> [String: Any] {
        var p: [String: Any] = ["prompt": d.idea.trimmingCharacters(in: .whitespacesAndNewlines),
                                "tags": d.tags, "language": d.language, "seconds": d.auto ? 0 : d.seconds,
                                "instrumental": d.lyricMode == .none,
                                "artists": d.artists.map { ["name": $0.name, "genres": $0.genres] },
                                "artwork": !d.album && d.artwork,
                                "explicit_lyrics": d.explicitLyrics]
        if !d.album, d.lyricMode == .mine { p["lyrics"] = d.lyrics.trimmingCharacters(in: .whitespacesAndNewlines) }
        if d.album {
            p["tracks"] = d.tracks
        } else if let ref {
            p["reference"] = ref.data.base64EncodedString()
            p["reference_ext"] = ref.ext
            p["reference_keep"] = d.keep
            if let s = ref.seconds { p["reference_seconds"] = s }
        } else {
            let link = d.link.trimmingCharacters(in: .whitespacesAndNewlines)
            if !link.isEmpty { p["reference_url"] = link; p["reference_keep"] = d.keep }
        }
        let label = d.idea.isEmpty ? (d.album ? "Album" : "Song") : d.idea
        p["label"] = String(label.prefix(60))
        return p
    }

    func make(_ d: SongDraft, ref: SongReference?) {
        run(d.album ? "album" : "song", payload(d, ref: ref), idea: d.idea)
    }

    /// The same song again with a new performance: no writing, same style, lyrics, length, tempo and key.
    func anotherTake(_ s: SongItem) {
        var p: [String: Any] = ["prompt": s.idea, "tags": s.tags, "lyrics": s.lyrics, "write": false,
                                "seconds": s.seconds, "title": s.title, "instrumental": s.instrumental,
                                "label": "Another take: " + s.displayTitle]
        if let b = s.bpm { p["bpm"] = b }
        if let k = s.key { p["key"] = k }
        if let l = s.language { p["language"] = l }
        p["explicit_lyrics"] = draft.explicitLyrics
        run("song", p, idea: s.idea)
    }

    private func run(_ op: String, _ p: [String: Any], idea: String) {
        let t = UUID()
        token = t
        note = nil
        albumPlan = nil
        status = "Sending to your computer…"
        progress = 0
        job?.cancel()
        job = Task {
            do {
                try await Desktop.shared.music(op, p, slot: "make:song") { [weak self] f in
                    guard let self, self.token == t else { return }
                    self.handle(f, idea: idea)
                }
            } catch is CancellationError {
            } catch {
                if token == t, !Task.isCancelled {
                    note = (error as? LocalizedError)?.errorDescription ?? "That didn't work. Try again."
                }
            }
            if token == t { status = nil; progress = 0; albumPlan = nil }
        }
    }

    private func handle(_ f: [String: Any], idea: String) {
        switch f["t"] as? String {
        case "status": status = f["v"] as? String ?? status
        case "progress": progress = f["v"] as? Double ?? progress
        case "note": note = f["v"] as? String
        case "album":
            albumPlan = (f["album_title"] as? String ?? "Album", f["titles"] as? [String] ?? [])
        case "cover":
            if let key = (f["album_id"] as? String) ?? (f["art_id"] as? String), let b64 = f["v"] as? String,
               let d = Data(base64Encoded: b64) {
                setArt(key, png: d)
            }
        case "audio":
            if let b64 = f["v"] as? String, let d = Data(base64Encoded: b64) { add(d, meta: f, idea: idea) }
        default: break
        }
    }

    func cancel() {
        job?.cancel(); job = nil
        token = nil
        status = nil; progress = 0; albumPlan = nil
        Desktop.shared.cancel(slot: "make:song")
    }

    /// "Write the lyrics first": fills the form with the computer's title, style and lyrics to read and change.
    func writeFirst() async {
        guard !writing else { return }
        writing = true
        note = nil
        defer { writing = false }
        do {
            var p = payload(draft, ref: nil)
            p["reference_url"] = nil
            let plan = try await Desktop.shared.songWrite(p)
            if let ly = plan["lyrics"] as? String, !ly.isEmpty, ly != "[Instrumental]" {
                draft.lyrics = ly
                draft.lyricMode = .mine
            }
            if let tags = plan["tags"] as? String, !tags.isEmpty { draft.style = tags; draft.picks = [:] }
            if let title = plan["title"] as? String, !title.isEmpty, draft.idea.isEmpty { draft.idea = title }
        } catch {
            note = (error as? LocalizedError)?.errorDescription ?? "Nothing was written. Try again."
        }
    }

    /// A finished song: written to the app's folder and put first in the list.
    func add(_ data: Data, meta: [String: Any], idea: String) {
        func int(_ k: String) -> Int? { (meta[k] as? NSNumber)?.intValue }
        let s = SongItem(title: meta["title"] as? String ?? "", idea: idea, tags: meta["tags"] as? String ?? "",
                         lyrics: meta["lyrics"] as? String ?? "", seconds: int("seconds") ?? 0,
                         file: UUID().uuidString + ".mp3", album: meta["album_id"] as? String,
                         albumTitle: meta["album_title"] as? String, track: int("track"), bpm: int("bpm"),
                         key: meta["key"] as? String, language: meta["language"] as? String, seed: int("seed"),
                         art: meta["art_id"] as? String)
        do { try data.write(to: url(of: s), options: .atomic) } catch { note = "The song couldn't be saved."; return }
        songs.insert(s, at: 0)
        save()
    }

    /// Finished while Fixer was closed: filed by JobInbox.
    func adopt(words: String, frames: [[String: Any]]) {
        for f in frames where f["t"] as? String == "cover" || f["t"] as? String == "audio" { handle(f, idea: words) }
    }

    func delete(_ s: SongItem) {
        if SongPlayer.shared.playing == s.id { SongPlayer.shared.stop() }
        try? FileManager.default.removeItem(at: url(of: s))
        songs.removeAll { $0.id == s.id }
        if s.album == nil || !songs.contains(where: { $0.album == s.album }) {
            try? FileManager.default.removeItem(at: coverURL(s.artKey))
        }
        save()
    }

    func deleteAlbum(_ id: String) {
        for s in songs where s.album == id { delete(s) }
        try? FileManager.default.removeItem(at: coverURL(id))
    }

    /// Erase everything with "songs too": every song, cover and the list.
    @discardableResult
    func deleteAll() -> Int {
        let n = songs.count
        SongPlayer.shared.stop()
        cancel()
        try? FileManager.default.removeItem(at: Self.folder)
        songs = []
        return n
    }
}

/// 11-20 (the owner: opening Fixer silenced his music and podcasts; Fixer songs stopped when he left the app).
/// Fixer mixes with other apps' audio and never interrupts it, except while one of its own songs is playing; when
/// that stops, the other app is told it can carry on.
enum AudioFocus {
    /// The everyday setting (set at launch): mix with whatever else is playing, never take over.
    static func mix() {
        let s = AVAudioSession.sharedInstance()
        try? s.setCategory(.ambient, mode: .default, options: [.mixWithOthers])
    }

    /// A song is playing: Fixer is the music now (keeps playing in the background and on the lock screen).
    static func take() {
        let s = AVAudioSession.sharedInstance()
        try? s.setCategory(.playback, mode: .default, options: [])
        try? s.setActive(true)
    }

    /// Done: hand the audio back, so the music or podcast that was playing resumes.
    static func release() {
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        mix()
    }
}

@MainActor
final class SongPlayer: NSObject, ObservableObject, AVAudioPlayerDelegate {
    static let shared = SongPlayer()
    @Published private(set) var playing: UUID?
    @Published private(set) var paused = false
    @Published var position: Double = 0
    private var player: AVAudioPlayer?
    private var timer: Timer?
    private var queue: [SongItem] = []
    private var played: [SongItem] = []                       // for "previous track"
    private var current: SongItem?
    private var commandsReady = false

    func toggle(_ s: SongItem) {
        if playing == s.id { if paused { resume() } else { stop() }; return }
        play([s])
    }

    /// Plays the songs one after another (an album's Play all).
    func play(_ list: [SongItem]) {
        stop(release: false)
        queue = list
        played = []
        next()
    }

    private func next() {
        guard !queue.isEmpty else { stop(); return }
        if let c = current { played.append(c) }
        start(queue.removeFirst())
    }

    private func previous() {
        if let p = player, p.currentTime > 3 { p.currentTime = 0; updateNowPlaying(); return }
        guard let last = played.popLast() else { player?.currentTime = 0; return }
        if let c = current { queue.insert(c, at: 0) }
        start(last)
    }

    private func start(_ s: SongItem) {
        player?.stop()
        AudioFocus.take()
        setUpCommands()
        guard let p = try? AVAudioPlayer(contentsOf: SongStore.shared.url(of: s)) else { current = nil; next(); return }
        p.delegate = self
        p.play()
        player = p
        current = s
        playing = s.id
        paused = false
        position = 0
        updateNowPlaying()
        if timer == nil {
            timer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
                Task { @MainActor in
                    guard let self, let p = self.player, p.duration > 0 else { return }
                    self.position = p.currentTime / p.duration
                }
            }
        }
    }

    func pause() {
        guard let p = player, !paused else { return }
        p.pause()
        paused = true
        updateNowPlaying()
    }

    func resume() {
        guard let p = player, paused else { return }
        AudioFocus.take()
        p.play()
        paused = false
        updateNowPlaying()
    }

    func stop() { stop(release: true) }

    private func stop(release: Bool) {
        let wasPlaying = player != nil
        queue = []
        played = []
        current = nil
        player?.stop(); player = nil
        timer?.invalidate(); timer = nil
        playing = nil; paused = false; position = 0
        MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
        if release && wasPlaying { AudioFocus.release() }
    }

    // MARK: lock screen and Control Center

    private func updateNowPlaying() {
        guard let s = current, let p = player else { return }
        var info: [String: Any] = [
            MPMediaItemPropertyTitle: s.displayTitle,
            MPMediaItemPropertyArtist: "Fixer",
            MPMediaItemPropertyPlaybackDuration: p.duration,
            MPNowPlayingInfoPropertyElapsedPlaybackTime: p.currentTime,
            MPNowPlayingInfoPropertyPlaybackRate: paused ? 0.0 : 1.0,
        ]
        if let a = s.albumTitle { info[MPMediaItemPropertyAlbumTitle] = a }
        if let n = s.track { info[MPMediaItemPropertyAlbumTrackNumber] = n }
        if let img = SongStore.shared.cover(s.artKey) {
            info[MPMediaItemPropertyArtwork] = MPMediaItemArtwork(boundsSize: img.size) { _ in img }
        }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
    }

    private func setUpCommands() {
        guard !commandsReady else { return }
        commandsReady = true
        let c = MPRemoteCommandCenter.shared()
        c.playCommand.addTarget { [weak self] _ in
            MainActor.assumeIsolated { self?.resume() }
            return .success
        }
        c.pauseCommand.addTarget { [weak self] _ in
            MainActor.assumeIsolated { self?.pause() }
            return .success
        }
        c.togglePlayPauseCommand.addTarget { [weak self] _ in
            MainActor.assumeIsolated { if self?.paused == true { self?.resume() } else { self?.pause() } }
            return .success
        }
        c.nextTrackCommand.addTarget { [weak self] _ in
            MainActor.assumeIsolated { self?.next() }
            return .success
        }
        c.previousTrackCommand.addTarget { [weak self] _ in
            MainActor.assumeIsolated { self?.previous() }
            return .success
        }
        c.changePlaybackPositionCommand.addTarget { [weak self] e in
            guard let e = e as? MPChangePlaybackPositionCommandEvent else { return .commandFailed }
            MainActor.assumeIsolated {
                self?.player?.currentTime = e.positionTime
                self?.updateNowPlaying()
            }
            return .success
        }
    }

    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor in self.next() }
    }
}

// MARK: - Screen

struct SongMakerView: View {
    @ObservedObject private var store = SongStore.shared
    @ObservedObject private var player = SongPlayer.shared
    @State private var pickGroup: SongPicks.Group?
    @State private var shownLyrics: UUID?
    @State private var openAlbums: Set<String> = []
    @State private var more = false
    @State private var artistQuery = ""
    @State private var artistHits: [ArtistPick] = []
    @State private var artistNote: String?
    @State private var reference: SongReference?
    @State private var importing = false
    @State private var artTarget: ArtTarget?                 // "Paint new artwork…" is asking for a wish
    @State private var artWish = ""
    @State private var photoKey: String?                     // "Choose from Photos" is open for this key
    @State private var photoItem: PhotosPickerItem?
    @FocusState private var focus: Bool

    struct ArtTarget: Identifiable {
        let id: String                                       // the artwork key
        let title, tags, lyrics, idea: String
    }

    private var d: Binding<SongDraft> { $store.draft }
    private let lengths: [(String, Int)] = [("30 s", 30), ("1 min", 60), ("2 min", 120), ("3 min", 180),
                                            ("4 min", 240), ("5 min", 300), ("6 min", 360)]

    var body: some View {
        ScrollView {
            VStack(spacing: 12) {
                kindSwitch
                form
                if let s = store.status { statusCard(s) }
                if let n = store.note { noteCard(n) }
                if store.songs.isEmpty && !store.running {
                    emptyCard
                } else {
                    ForEach(store.entries) { e in
                        switch e {
                        case .song(let s): songRow(s).swipeToDelete(s.displayTitle) { store.delete(s) }
                        case .album(let id, let title, let tracks):
                            albumCard(id, title, tracks)
                                .swipeToDelete(title, enabled: !openAlbums.contains(id)) { store.deleteAlbum(id) }
                        }
                    }
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
        .safeAreaInset(edge: .bottom) { bottomBar }
        .sheet(item: $pickGroup) { g in
            QuickPickSheet(title: g.title,
                           options: g.options.map { PickOption(id: $0, label: $0, asset: nil, explicit: false) },
                           isOn: { (store.draft.picks[g.id] ?? []).contains($0) },
                           toggle: { togglePick(g.id, $0) })
        }
        .fileImporter(isPresented: $importing, allowedContentTypes: [.audio]) { result in
            if case .success(let url) = result { Task { await loadReference(url) } }
        }
        .task(id: artistQuery) { await searchArtists() }
        .alert("Paint new artwork", isPresented: Binding(get: { artTarget != nil }, set: { if !$0 { artTarget = nil } })) {
            TextField("Describe it (optional)", text: $artWish)
            Button("Paint") {
                if let t = artTarget {
                    store.paintArt(key: t.id, title: t.title, tags: t.tags, lyrics: t.lyrics, idea: t.idea,
                                   wish: artWish.trimmingCharacters(in: .whitespacesAndNewlines))
                }
                artTarget = nil
            }
            Button("Cancel", role: .cancel) { artTarget = nil }
        } message: {
            Text("Leave it empty and it's designed from the song.")
        }
        .photosPicker(isPresented: Binding(get: { photoKey != nil }, set: { if !$0 { photoKey = nil } }),
                      selection: $photoItem, matching: .images)
        .onChange(of: photoItem) { _, item in
            guard let item, let key = photoKey else { return }
            Task {
                if let data = try? await item.loadTransferable(type: Data.self), let img = UIImage(data: data) {
                    store.setArt(key, image: img)
                }
                photoItem = nil
                photoKey = nil
            }
        }
    }

    private func paint(_ key: String, _ title: String, _ songs: [SongItem]) {
        let first = songs.first
        artWish = ""
        artTarget = ArtTarget(id: key, title: title, tags: first?.tags ?? "",
                              lyrics: songs.map(\.lyrics).filter { $0 != "[Instrumental]" }.joined(separator: "\n\n"),
                              idea: first?.idea ?? "")
    }

    @ViewBuilder private func artMenu(_ key: String, _ title: String, _ songs: [SongItem]) -> some View {
        Button("Paint new artwork…", systemImage: "paintbrush") { paint(key, title, songs) }
        Button("Choose artwork from Photos", systemImage: "photo") { photoKey = key }
        if store.cover(key) != nil {
            Button("Remove artwork", systemImage: "photo.badge.minus") { store.removeArt(key) }
        }
    }

    /// The artwork (or a placeholder; a spinner while it is being painted).
    private func artThumb(_ key: String, side: CGFloat, placeholder: String) -> some View {
        ZStack {
            if let c = store.cover(key) {
                Image(uiImage: c).resizable().scaledToFill()
            } else {
                Fx.canvas
                Image(systemName: placeholder).font(.system(size: side * 0.36)).foregroundStyle(Fx.secondaryText)
            }
            if store.painting.contains(key) {
                Color.black.opacity(0.35)
                ProgressView().tint(.white)
            }
        }
        .frame(width: side, height: side)
        .clipShape(RoundedRectangle(cornerRadius: side > 50 ? 10 : 7, style: .continuous))
        .accessibilityHidden(true)
    }

    // MARK: form

    private var kindSwitch: some View {
        Picker("Make", selection: d.album) {
            Text("Song").tag(false)
            Text("Album").tag(true)
        }
        .pickerStyle(.segmented)
        .frame(minHeight: 44)
    }

    private var form: some View {
        VStack(alignment: .leading, spacing: 12) {
            field(store.draft.album ? "What's the album? A theme, a story, a feeling…"
                                    : "What's the song? What it's about, the feel…", text: d.idea, lines: 2...6)
            artistsSection
            soundSection
            if store.draft.album {
                albumOptions
            } else {
                lyricsSection
                referenceSection
                Toggle(isOn: d.artwork) {
                    Label("Make artwork for it", systemImage: "photo.artframe").font(.subheadline.weight(.semibold))
                }
                .tint(Fx.accent)
                .frame(minHeight: 44)
            }
            moreSection
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 20, style: .continuous).fill(Fx.tile))
    }

    private func field(_ prompt: String, text: Binding<String>, lines: ClosedRange<Int>) -> some View {
        TextField(prompt, text: text, axis: .vertical)
            .lineLimit(lines)
            .focused($focus)
            .padding(12)
            .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Fx.canvas))
    }

    private func header(_ t: String, _ icon: String) -> some View {
        Label(t, systemImage: icon).font(.subheadline.weight(.semibold)).padding(.top, 2)
    }

    // Sounds like (MusicBrainz)
    private var artistsSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            header("Sounds like", "person.wave.2")
            if !store.draft.artists.isEmpty {
                FlowRows(spacing: 6) {
                    ForEach(store.draft.artists) { a in
                        chip(a.name, on: true) { store.draft.artists.removeAll { $0.id == a.id } }
                    }
                }
            }
            if store.draft.artists.count < 5 {
                TextField("Search an artist or band", text: $artistQuery)
                    .textInputAutocapitalization(.words)
                    .autocorrectionDisabled()
                    .focused($focus)
                    .padding(12)
                    .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Fx.canvas))
                ForEach(artistHits) { a in
                    Button {
                        if !store.draft.artists.contains(where: { $0.id == a.id }) { store.draft.artists.append(a) }
                        artistQuery = ""
                        artistHits = []
                    } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(a.name).font(.subheadline.weight(.semibold))
                            let line = ([a.detail].compactMap { $0 } + [a.genres.prefix(3).joined(separator: ", ")])
                                .filter { !$0.isEmpty }.joined(separator: " · ")
                            if !line.isEmpty { Text(line).font(.caption).foregroundStyle(Fx.secondaryText).lineLimit(1) }
                        }
                        .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
                if let n = artistNote { Text(n).font(.caption).foregroundStyle(Fx.secondaryText) }
            }
        }
    }

    private func searchArtists() async {
        let q = artistQuery
        guard q.trimmingCharacters(in: .whitespaces).count >= 2 else { artistHits = []; artistNote = nil; return }
        try? await Task.sleep(nanoseconds: 450_000_000)        // typing pause (MusicBrainz asks for 1 a second)
        guard !Task.isCancelled else { return }
        do {
            let hits = try await MusicBrainz.search(q)
            guard !Task.isCancelled, q == artistQuery else { return }
            artistHits = hits
            artistNote = hits.isEmpty ? "No artist found." : nil
        } catch {
            if !Task.isCancelled { artistNote = "Couldn't reach MusicBrainz." }
        }
    }

    // Sound picks
    private var soundSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            header("Sound", "slider.horizontal.3")
            ForEach(SongPicks.groups) { g in
                let picked = store.draft.picks[g.id] ?? []
                HStack(alignment: .top, spacing: 8) {
                    Text(g.title)
                        .font(.footnote.weight(.semibold))
                        .foregroundStyle(Fx.secondaryText)
                        .fixedSize()
                        .frame(minWidth: 84, minHeight: 34, alignment: .leading)
                    FlowRows(spacing: 6) {
                        addButton(g)
                        ForEach(picked, id: \.self) { o in chip(o, on: true) { togglePick(g.id, o) } }
                    }
                }
            }
        }
    }

    private func addButton(_ g: SongPicks.Group) -> some View {
        Button { pickGroup = g } label: {
            Image(systemName: "square.grid.2x2")
                .font(.system(size: 14, weight: .semibold))
                .foregroundStyle(Color.primary)
                .frame(width: 44, height: 34)
                .background(Capsule().fill(Fx.canvas))
                .frame(minHeight: 44)
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Choose \(g.title)")
    }

    private func chip(_ t: String, on: Bool, tap: @escaping () -> Void) -> some View {
        Button(action: tap) {
            HStack(spacing: 5) {
                Text(t).font(.system(size: 14, weight: .semibold)).lineLimit(1)
                if on { Image(systemName: "xmark").font(.system(size: 10, weight: .bold)) }
            }
            .foregroundStyle(on ? Fx.accentSoftText : Color.primary)
            .padding(.horizontal, 12)
            .frame(height: 34)
            .background(Capsule().fill(on ? Fx.onTintStrong : Fx.canvas))
            .overlay(Capsule().strokeBorder(on ? Fx.accent : .clear, lineWidth: 1))
            .frame(minHeight: 44)
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityHint(on ? "Remove" : "Add")
    }

    private func togglePick(_ group: String, _ o: String) {
        var list = store.draft.picks[group] ?? []
        if let i = list.firstIndex(of: o) { list.remove(at: i) } else { list.append(o) }
        store.draft.picks[group] = list.isEmpty ? nil : list
    }

    // Lyrics
    private var lyricsSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            header("Lyrics", "text.quote")
            Picker("Lyrics", selection: d.lyricMode) {
                Text("Write them").tag(SongDraft.Lyrics.written)
                Text("Mine").tag(SongDraft.Lyrics.mine)
                Text("Instrumental").tag(SongDraft.Lyrics.none)
            }
            .pickerStyle(.segmented)
            .frame(minHeight: 44)
            switch store.draft.lyricMode {
            case .written:
                Button { Task { await store.writeFirst() } } label: {
                    HStack(spacing: 8) {
                        if store.writing { ProgressView() } else { Image(systemName: "pencil.and.scribble") }
                        Text(store.writing ? "Writing…" : "Write them now, to read and change first")
                            .font(.footnote.weight(.semibold))
                    }
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .background(Capsule().fill(Fx.canvas))
                    .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .disabled(store.writing || (store.draft.idea.isEmpty && store.draft.tags.isEmpty))
            case .mine:
                field("Your lyrics. Section labels like [Verse], [Chorus], [Bridge] help.", text: d.lyrics, lines: 4...16)
            case .none:
                EmptyView()
            }
            if store.draft.lyricMode != .none { explicitToggle }
        }
    }

    /// 11-20: written lyrics may swear (and, while 18+ is on, go adult). One saved switch.
    private var explicitToggle: some View {
        Toggle(isOn: d.explicitLyrics) {
            Label("Explicit lyrics", systemImage: "e.square").font(.subheadline.weight(.semibold))
        }
        .tint(Fx.accent)
        .frame(minHeight: 44)
        .accessibilityHint("Off keeps written lyrics clean")
    }

    // Start from a recording
    private var referenceSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            header("Start from a recording", "waveform")
            if let r = reference {
                chip("\(r.name)\(r.seconds.map { " · \(Self.length($0))" } ?? "")", on: true) { reference = nil }
            } else {
                HStack(spacing: 8) {
                    Button { importing = true } label: {
                        Label("A file", systemImage: "folder").font(.footnote.weight(.semibold))
                            .frame(maxWidth: .infinity, minHeight: 44)
                            .background(Capsule().fill(Fx.canvas))
                            .contentShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    Menu {
                        ForEach(store.songs.prefix(30)) { s in
                            Button(s.displayTitle) { useSong(s) }
                        }
                    } label: {
                        Label("One of my songs", systemImage: "music.note.list").font(.footnote.weight(.semibold))
                            .foregroundStyle(Color.primary)
                            .frame(maxWidth: .infinity, minHeight: 44)
                            .background(Capsule().fill(Fx.canvas))
                            .contentShape(Capsule())
                    }
                    .disabled(store.songs.isEmpty)
                }
                TextField("…or a link (YouTube, SoundCloud, Bandcamp…)", text: d.link)
                    .keyboardType(.URL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .focused($focus)
                    .padding(12)
                    .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Fx.canvas))
            }
            if reference != nil || !store.draft.link.trimmingCharacters(in: .whitespaces).isEmpty {
                Picker("How close to the recording", selection: d.keep) {
                    Text("Loose").tag("loose")
                    Text("Balanced").tag("balanced")
                    Text("Close").tag("close")
                }
                .pickerStyle(.segmented)
                .frame(minHeight: 44)
                Text("The song is written from your lyrics, picks and artists, then made starting from the recording: "
                     + "Close keeps more of its melody and sound, Loose changes more. Used for this song only and "
                     + "deleted from your computer after.")
                    .font(.caption).foregroundStyle(Fx.secondaryText)
            }
        }
    }

    private func useSong(_ s: SongItem) {
        guard let data = try? Data(contentsOf: store.url(of: s)) else { return }
        reference = SongReference(name: s.displayTitle, data: data, ext: "mp3", seconds: s.seconds > 0 ? s.seconds : nil)
        store.draft.link = ""
        store.draft.album = false
    }

    private func loadReference(_ url: URL) async {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        guard let data = try? Data(contentsOf: url) else { store.note = "That file couldn't be read."; return }
        guard data.count <= 30 * 1024 * 1024 else { store.note = "That recording is too large (30 MB at most)."; return }
        let secs = try? await AVURLAsset(url: url).load(.duration).seconds
        reference = SongReference(name: url.deletingPathExtension().lastPathComponent, data: data,
                                  ext: url.pathExtension.lowercased(),
                                  seconds: secs.flatMap { $0.isFinite && $0 > 0 ? Int($0) : nil })
        store.draft.link = ""
    }

    // Album
    private var albumOptions: some View {
        VStack(alignment: .leading, spacing: 8) {
            Stepper(value: d.tracks, in: 2...12) {
                Label("\(store.draft.tracks) tracks", systemImage: "square.stack").font(.subheadline.weight(.semibold))
            }
            .frame(minHeight: 44)
            Toggle(isOn: Binding(get: { store.draft.lyricMode == .none },
                                 set: { store.draft.lyricMode = $0 ? .none : .written })) {
                Label("Instrumental album", systemImage: "pianokeys").font(.subheadline.weight(.semibold))
            }
            .tint(Fx.accent)
            .frame(minHeight: 44)
            if store.draft.lyricMode != .none { explicitToggle }
            Text("Each track is written and sung in turn, with a cover; they arrive one by one, so the first ones "
                 + "play while the rest are made. About 1–2 minutes a track.")
                .font(.caption).foregroundStyle(Fx.secondaryText)
        }
    }

    // Length, language, style line
    private var moreSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button { withAnimation(Motion.pop) { more.toggle() } } label: {
                HStack {
                    Label("Length, language, style", systemImage: "gearshape").font(.subheadline.weight(.semibold))
                    Spacer()
                    Text(store.draft.auto ? "Auto" : Self.length(store.draft.seconds))
                        .font(.footnote).foregroundStyle(Fx.secondaryText)
                    Image(systemName: more ? "chevron.up" : "chevron.down").font(.footnote.weight(.semibold))
                }
                .foregroundStyle(Color.primary)
                .frame(minHeight: 44)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if more {
                Toggle(isOn: d.auto) {
                    Text(store.draft.album ? "Auto length for each track" : "Auto length (fits the song)")
                        .font(.subheadline)
                }
                .tint(Fx.accent)
                .frame(minHeight: 44)
                if !store.draft.auto {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(Self.length(store.draft.seconds)).font(.footnote.weight(.semibold)).monospacedDigit()
                        Slider(value: Binding(get: { Double(store.draft.seconds) },
                                              set: { store.draft.seconds = Int(($0 / 15).rounded()) * 15 }),
                               in: 15...360, step: 15)
                            .tint(Fx.accent)
                    }
                }
                Picker(selection: d.language) {
                    ForEach(SongPicks.languages, id: \.0) { l in Text(l.1).tag(l.0) }
                } label: {
                    Text("Singing language").font(.subheadline)
                }
                .frame(minHeight: 44)
                field("Style, in your own words (optional): \"dusty 70s soul, horn section, warm tape\"",
                      text: d.style, lines: 1...4)
            }
        }
    }

    private func statusCard(_ s: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                ProgressView()
                Text(s).font(.subheadline.weight(.semibold))
                Spacer()
            }
            if store.progress > 0 { ProgressView(value: store.progress).tint(Fx.accent) }
            if let plan = store.albumPlan {
                Text(plan.title).font(.footnote.weight(.semibold))
                ForEach(Array(plan.titles.enumerated()), id: \.offset) { i, t in
                    Text("\(i + 1). \(t)").font(.caption).foregroundStyle(Fx.secondaryText)
                }
            }
        }
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Fx.tile))
    }

    private func noteCard(_ n: String) -> some View {
        HStack(alignment: .top) {
            Text(n).font(.footnote).foregroundStyle(Fx.secondaryText).padding(.vertical, 12)
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

    // MARK: list

    private func albumCard(_ id: String, _ title: String, _ tracks: [SongItem]) -> some View {
        let open = openAlbums.contains(id)
        let total = tracks.reduce(0) { $0 + $1.seconds }
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                artThumb(id, side: 72, placeholder: "square.stack")
                VStack(alignment: .leading, spacing: 3) {
                    Text(title).font(.subheadline.weight(.semibold)).lineLimit(2)
                    Text("Album · \(tracks.count) tracks · \(Self.length(total))")
                        .font(.caption).foregroundStyle(Fx.secondaryText)
                    HStack(spacing: 6) {
                        Button { player.play(tracks) } label: {
                            Label("Play all", systemImage: "play.fill").font(.footnote.weight(.semibold))
                                .foregroundStyle(Fx.onSolid)
                                .padding(.horizontal, 12).frame(minHeight: 34)
                                .background(Capsule().fill(Fx.solid))
                                .frame(minHeight: 44).contentShape(Capsule())
                        }
                        .buttonStyle(.plain)
                        Button { withAnimation(Motion.pop) { if open { openAlbums.remove(id) } else { openAlbums.insert(id) } } } label: {
                            Text(open ? "Hide tracks" : "Tracks").font(.footnote.weight(.semibold))
                                .padding(.horizontal, 12).frame(minHeight: 34)
                                .background(Capsule().fill(Fx.canvas))
                                .frame(minHeight: 44).contentShape(Capsule())
                        }
                        .buttonStyle(.plain)
                        .foregroundStyle(Color.primary)
                        Spacer(minLength: 0)
                        Menu {
                            artMenu(id, title, tracks)
                            Button("Delete album", systemImage: "trash", role: .destructive) { store.deleteAlbum(id) }
                        } label: {
                            Image(systemName: "ellipsis").frame(width: 44, height: 44).contentShape(Rectangle())
                        }
                        .foregroundStyle(Color.primary)
                        .accessibilityLabel("More for \(title)")
                    }
                }
            }
            if open {
                ForEach(tracks) { s in songRow(s, inAlbum: true).swipeToDelete(s.displayTitle) { store.delete(s) } }
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Fx.tile))
    }

    @ViewBuilder private func songRow(_ s: SongItem, inAlbum: Bool = false) -> some View {
        let on = player.playing == s.id
        VStack(alignment: .leading, spacing: 8) {
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
                if !inAlbum { artThumb(s.artKey, side: 44, placeholder: "music.note") }
                // 11-20 (the owner): tapping the song shows and hides its lyrics, no menu needed.
                Button {
                    guard !s.instrumental else { return }
                    withAnimation(Motion.pop) { shownLyrics = shownLyrics == s.id ? nil : s.id }
                } label: {
                    HStack(spacing: 0) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text((s.track.map { "\($0). " } ?? "") + s.displayTitle)
                                .font(.subheadline.weight(.semibold)).lineLimit(1)
                            Text("\(Self.length(s.seconds)) · \(s.tags)").font(.caption)
                                .foregroundStyle(Fx.secondaryText).lineLimit(1)
                        }
                        Spacer(minLength: 0)
                    }
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityHint(s.instrumental ? "Instrumental" : (shownLyrics == s.id ? "Hides the lyrics" : "Shows the lyrics"))
                ShareLink(item: store.url(of: s)) {
                    Image(systemName: "square.and.arrow.up").frame(width: 44, height: 44)
                }
                .accessibilityLabel("Share \(s.displayTitle)")
                Menu {
                    Button("Another take", systemImage: "arrow.triangle.2.circlepath") { store.anotherTake(s) }
                        .disabled(store.running)
                    Button("Edit and remake", systemImage: "slider.horizontal.3") { editAndRemake(s) }
                    Button("Start a new song from this one", systemImage: "waveform") { useSong(s) }
                    if !inAlbum { artMenu(s.artKey, s.displayTitle, [s]) }
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
        .padding(inAlbum ? 0 : 12)
        .background(Fx.tile)                         // opaque, so a swipe slides it over the red Delete
        .clipShape(RoundedRectangle(cornerRadius: inAlbum ? 0 : 16, style: .continuous))
    }

    /// The song's style and lyrics back in the form, to change and make again.
    private func editAndRemake(_ s: SongItem) {
        var dr = store.draft
        dr.album = false
        dr.idea = s.idea
        dr.style = s.tags
        dr.picks = [:]
        if s.instrumental { dr.lyricMode = .none } else { dr.lyrics = s.lyrics; dr.lyricMode = .mine }
        dr.auto = false
        dr.seconds = max(15, min(360, s.seconds))
        if let l = s.language { dr.language = l }
        store.draft = dr
        more = true
    }

    static func length(_ s: Int) -> String { s >= 60 ? "\(s / 60):" + String(format: "%02d", s % 60) : "\(s) s" }

    // MARK: button

    private var bottomBar: some View {
        let stop = store.running
        let dr = store.draft
        let ready = !dr.idea.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !dr.tags.isEmpty
            || !dr.artists.isEmpty || (!dr.album && dr.lyricMode == .mine && !dr.lyrics.isEmpty)
        return Button {
            if stop { store.cancel(); return }
            focus = false
            store.make(store.draft, ref: store.draft.album ? nil : reference)
        } label: {
            HStack(spacing: 8) {
                Image(systemName: stop ? "stop.fill" : (dr.album ? "square.stack" : "music.note"))
                    .font(.system(size: stop ? 12 : 16, weight: .semibold))
                Text(stop ? "Stop" : (dr.album ? "Make album" : "Make song")).font(.body.weight(.semibold))
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


// MARK: - Swipe to delete

/// 11-20 (the owner: "need to swipe to delete songs quickly"). The song list is not a List, so this is the iOS
/// gesture by hand: swipe left to show Delete (tap it), or swipe all the way to delete at once. Vertical scrolling
/// is untouched: only a mostly sideways drag moves the row.
private struct SwipeToDelete: ViewModifier {
    let title: String
    let enabled: Bool
    let onDelete: () -> Void
    @State private var offset: CGFloat = 0
    @State private var width: CGFloat = 320
    private let reveal: CGFloat = 88

    func body(content: Content) -> some View {
        ZStack(alignment: .trailing) {
            if offset < 0 {
                Button(role: .destructive) { remove() } label: {
                    Label("Delete", systemImage: "trash")
                        .labelStyle(.iconOnly)
                        .font(.system(size: 18, weight: .semibold))
                        .foregroundStyle(.white)
                        .frame(width: max(reveal, -offset), alignment: .center)
                        .frame(maxHeight: .infinity)
                }
                .buttonStyle(.plain)
                .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Fx.danger))
                .accessibilityLabel("Delete \(title)")
            }
            content
                .offset(x: offset)
                .background(GeometryReader { g in Color.clear.onAppear { width = g.size.width } })
                .simultaneousGesture(
                    DragGesture(minimumDistance: 18)
                        .onChanged { v in
                            guard enabled, abs(v.translation.width) > abs(v.translation.height) * 1.4 else { return }
                            let base: CGFloat = offset <= -reveal + 1 && v.translation.width > 0 ? -reveal : 0
                            offset = min(0, base + v.translation.width)
                        }
                        .onEnded { v in
                            guard enabled else { return }
                            if offset < -width * 0.6 {
                                remove()
                            } else {
                                withAnimation(Motion.pop) { offset = offset < -reveal * 0.5 ? -reveal : 0 }
                            }
                        }
                )
        }
        .accessibilityAction(named: "Delete") { remove() }
    }

    private func remove() {
        withAnimation(Motion.pop) { offset = -width }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.18) {
            onDelete()
            offset = 0
        }
    }
}

extension View {
    func swipeToDelete(_ title: String, enabled: Bool = true, _ onDelete: @escaping () -> Void) -> some View {
        modifier(SwipeToDelete(title: title, enabled: enabled, onDelete: onDelete))
    }
}
