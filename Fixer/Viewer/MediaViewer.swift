import AVFoundation
import Photos
import SwiftUI
import UIKit

// MARK: - What the viewer shows

/// One picture or video the viewer can page through, wherever it lives (a chat, the Library, this session's Make
/// results). The id is the host's own id for it (a chat message, a Library item, a Make result).
struct MediaItem: Identifiable {
    enum Kind { case picture, video }
    enum Source {
        case chat(conv: UUID, file: String)
        case library(UUID)
        case memory(UIImage)
        case sessionPicture(UUID)       // 11-22: a Make result; its pixels are faulted from MakeMedia on demand
        case chatVideo(URL)
    }
    let id: UUID
    var kind: Kind
    var source: Source
    var libraryID: UUID? = nil          // its sealed copy in the Library, when it has one (Protect works on it)
    var recipeID: String? = nil         // the Make model that drew it, when known
    var note: String? = nil             // a line shown under the picture ("Revised: ...")
    var enhanced: String? = nil         // 11-17: the prompt the computer wrote from the owner's words, when Enhance was on
}

/// How a "Change" should be made when the picture has a Make model behind it.
struct MediaChange {
    var mode = "edit"                   // edit: the photo editor keeps the rest. redraw: the picture's own model.
    var strength: Double?               // how much a painted spot may change (nil = the computer's choice)
}

/// What a host lets the owner do with an item. A nil closure hides its button.
struct MediaActions {
    var change: (@MainActor (MediaItem, String, CGImage?, MediaChange) -> Void)?
    var remove: (@MainActor (MediaItem, CGImage?) -> Void)?
    var cutOut: (@MainActor (MediaItem, CGImage?) -> Void)?
    var upscale: (@MainActor (MediaItem) -> Void)?
    var animate: (@MainActor (MediaItem, String) -> Void)?
    var makeAnother: (@MainActor (MediaItem) -> Void)?
    var toChat: (@MainActor (MediaItem) -> Void)?
    var delete: (@MainActor (MediaItem) -> Void)?
    var undo: (@MainActor () -> Void)?              // goes back one change (chat only)
    var canUndo = false
    var cancel: (@MainActor () -> Void)?            // stops the work the host is busy with
    var closesAfterAction = false                    // the chat shows progress in the conversation, so the viewer closes
}

/// Opens pictures and videos for the pager, one at a time as they come on screen. Videos from the Library are
/// decrypted into a temporary copy, removed when the viewer closes.
@MainActor
final class MediaLoader: ObservableObject {
    @Published private(set) var images: [UUID: UIImage] = [:]
    @Published private(set) var videos: [UUID: URL] = [:]
    @Published private(set) var failed: Set<UUID> = []
    private var temp: Set<UUID> = []

    func load(_ item: MediaItem) async {
        guard images[item.id] == nil, videos[item.id] == nil else { return }
        switch item.source {
        case .memory(let ui):
            images[item.id] = ui
        case .sessionPicture(let rid):
            // Decode off the main thread; only the on-screen result is faulted, so a big session never decodes them all.
            images[item.id] = await Task.detached(priority: .userInitiated) { MakeMedia.shared.image(rid) }.value
        case .chat(let conv, let file):
            if let ui = History.image(file, in: conv) { images[item.id] = ui } else { failed.insert(item.id) }
        case .chatVideo(let url):
            videos[item.id] = url
        case .library(let lid):
            if item.kind == .picture {
                if let ui = await LibraryStore.shared.image(lid) { images[item.id] = ui } else { failed.insert(item.id) }
            } else if let url = await LibraryStore.shared.videoURL(lid) {
                temp.insert(lid)
                videos[item.id] = url
            } else {
                failed.insert(item.id)
            }
        }
    }

    func cleanUp() {
        for id in temp { LibraryStore.shared.removeTempVideo(id) }
        temp = []
    }
}

// MARK: - The viewer

/// The one full-screen viewer: swipe between pictures and videos, pinch or double-tap to zoom, paint a spot and say
/// what to change, remove, cut out, upscale, animate, make another, save, protect. The host decides which of those
/// exist (chat, Make or Library) by what it puts in `actions`.
struct MediaViewer: View {
    let items: [MediaItem]
    @Binding var current: UUID
    let actions: MediaActions
    var busy: String?                   // the host's work in progress ("Revising…")
    var note: String?                   // the host's message ("1 picture blocked…")

    @Environment(\.dismiss) private var dismiss
    @ObservedObject private var library = LibraryStore.shared
    @StateObject private var loader = MediaLoader()
    @State private var painting = false
    @State private var erasing = false
    @State private var brush: CGFloat = 40
    @State private var strokes: [Stroke] = []
    @State private var paintOverlay: UIImage?
    @State private var words = ""
    @State private var changeMode = "edit"
    @State private var amount: Double?
    @State private var localNote: String?
    @State private var confirmDelete = false
    @State private var askAnimate = false
    @State private var animateWords = ""
    @FocusState private var typing: Bool

    private var item: MediaItem? { items.first { $0.id == current } }
    private var image: UIImage? { item.flatMap { loader.images[$0.id] } }

    var body: some View {
        ZStack(alignment: .topTrailing) {
            Color.black.ignoresSafeArea()
            if painting, let image {
                BrushCanvas(image: image, maskOverlay: paintOverlay, painting: busy == nil, erasing: erasing,
                            brushSize: brush, strokes: $strokes)
            } else {
                TabView(selection: $current) {
                    ForEach(items) { page($0).tag($0.id) }
                }
                .tabViewStyle(.page(indexDisplayMode: items.count > 1 ? .automatic : .never))
                .ignoresSafeArea()
            }
            Button { dismiss() } label: {
                Text("Done").font(.body.weight(.semibold)).padding(.horizontal, 14)
                    .frame(minHeight: 44)
                    .background(.ultraThinMaterial, in: Capsule())
            }
            .padding(.trailing, 12).padding(.top, 4)
            .accessibilityLabel("Done")
        }
        .environment(\.colorScheme, .dark)
        .safeAreaInset(edge: .bottom, spacing: 0) { bottomBar }
        .onChange(of: strokes) { _, s in
            guard let image,
                  let m = BrushMask.render(s, size: CGSize(width: image.size.width * image.scale,
                                                           height: image.size.height * image.scale)) else {
                paintOverlay = nil; return
            }
            paintOverlay = ImageKit.overlay(m)
        }
        .onChange(of: current) { _, _ in
            strokes = []; painting = false; words = ""; localNote = nil
        }
        .onChange(of: items.isEmpty) { _, empty in if empty { dismiss() } }
        .onDisappear { loader.cleanUp() }
        .confirmationDialog(deleteTitle, isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("Delete", role: .destructive) { deleteCurrent() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This can't be undone.")
        }
        .sheet(isPresented: $askAnimate) { animateSheet }
    }

    // MARK: pages

    @ViewBuilder private func page(_ it: MediaItem) -> some View {
        Group {
            if it.kind == .picture, let ui = loader.images[it.id] {
                ZoomableImage(image: ui).ignoresSafeArea()
            } else if it.kind == .video, let url = loader.videos[it.id] {
                VideoPage(url: url, active: it.id == current)
            } else if loader.failed.contains(it.id) {
                Text("That item couldn't be opened.").foregroundStyle(.white.opacity(0.7))
            } else {
                ProgressView().tint(.white)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .task { await loader.load(it) }
    }

    // MARK: bottom bar

    private var bottomBar: some View {
        VStack(spacing: 8) {
            if let caption = item?.note {
                Text(caption).font(.caption).foregroundStyle(.white.opacity(0.7)).lineLimit(2)
            }
            if let enhanced = item?.enhanced, !enhanced.isEmpty, !painting {
                DisclosureGroup {
                    ScrollView { Text(enhanced).font(.footnote).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                        .frame(maxHeight: 140)
                } label: {
                    Label("Enhanced prompt", systemImage: "sparkles").font(.footnote).foregroundStyle(.white.opacity(0.8))
                }
                .padding(.horizontal)
            }
            if busy != nil || (localNote ?? note) != nil {
                HStack(spacing: 8) {
                    if let busy {
                        ProgressView().tint(.white)
                        Text(busy).font(.footnote)
                        if let cancel = actions.cancel {
                            Button { cancel() } label: { Image(systemName: "stop.circle.fill").iconButton() }
                                .accessibilityLabel("Stop")
                        }
                    } else if let text = localNote ?? note {
                        Text(text).font(.footnote).foregroundStyle(.white.opacity(0.85))
                    }
                }
            }
            if painting { paintControls }
            chips
        }
        .padding(.top, 10).padding(.bottom, 6)
        .frame(maxWidth: .infinity)
        .background(.ultraThinMaterial)
    }

    private var paintControls: some View {
        VStack(spacing: 6) {
            HStack(spacing: 10) {
                Picker("Brush", selection: $erasing) {
                    Text("Paint").tag(false)
                    Text("Erase").tag(true)
                }
                .pickerStyle(.segmented).frame(width: 150)
                Slider(value: $brush, in: 12...120)
                Button { strokes = [] } label: { Image(systemName: "xmark.circle").iconButton() }
                    .disabled(strokes.isEmpty)
                    .accessibilityLabel("Clear painting")
            }
            if actions.change != nil, item?.recipeID != nil, strokes.isEmpty {
                Picker("How", selection: $changeMode) {
                    Text("Keep the rest").tag("edit")
                    Text("Its own model").tag("redraw")
                }
                .pickerStyle(.segmented)
            }
            // Qwen-Image 2.1 repaints the spot at full strength (it has no "how much" setting).
            if let recipe = item?.recipeID, recipe != "qwen21", !strokes.isEmpty, actions.change != nil {
                HStack(spacing: 8) {
                    Text("Change").font(.caption).foregroundStyle(.white.opacity(0.8))
                    Slider(value: Binding(get: { amount ?? 0.9 }, set: { amount = $0 }), in: 0.3...1.0, step: 0.05)
                    Text(amount.map { "\(Int($0 * 100))%" } ?? "Auto").font(.caption).monospacedDigit()
                        .foregroundStyle(.white.opacity(0.8)).frame(width: 40)
                }
            }
            HStack(spacing: 8) {
                TextField(strokes.isEmpty ? "Change: describe it"
                                          : (actions.remove != nil ? "What goes here? Empty removes it" : "What goes here?"),
                          text: $words)
                    .textFieldStyle(.roundedBorder)
                    .focused($typing)
                    .submitLabel(.send)
                    .onSubmit { submit() }
                Button { submit() } label: { Image(systemName: "arrow.up.circle.fill").font(.title2).iconButton() }
                    .disabled(busy != nil)
                    .accessibilityLabel("Make the change")
            }
        }
        .padding(.horizontal)
    }

    private var chips: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                if let item {
                    if item.kind == .picture {
                        if actions.change != nil {
                            ToolChip(title: painting ? "Cancel" : "Change", icon: painting ? "xmark" : "paintbrush.pointed",
                                     on: painting) { togglePainting() }
                        }
                        if let undo = actions.undo, actions.canUndo {
                            ToolChip(title: "Undo", icon: "arrow.uturn.backward", on: false) { undo(); finish() }
                        }
                        if actions.remove != nil {
                            ToolChip(title: "Remove", icon: "eraser", on: false) { removeTapped(item) }
                        }
                        if let cutOut = actions.cutOut {
                            ToolChip(title: "Cut out", icon: "person.crop.rectangle", on: false) {
                                cutOut(item, currentMask()); finish()
                            }
                        }
                        if let upscale = actions.upscale {
                            ToolChip(title: "Upscale", icon: "arrow.up.left.and.arrow.down.right", on: false) {
                                upscale(item); finish()
                            }
                        }
                        if actions.animate != nil {
                            ToolChip(title: "Animate", icon: "play.circle", on: false) { animateWords = ""; askAnimate = true }
                        }
                        if let toChat = actions.toChat {
                            ToolChip(title: "To chat", icon: "bubble.left.and.bubble.right", on: false) {
                                toChat(item); finish()
                            }
                        }
                    }
                    if let again = actions.makeAnother {
                        ToolChip(title: "Make another", icon: "slider.horizontal.3", on: false) { again(item); finish() }
                    }
                    ToolChip(title: "Save", icon: "square.and.arrow.down", on: false) { save() }
                    if let lid = item.libraryID {
                        let kept = isProtected(lid)
                        ToolChip(title: kept ? "Unprotect" : "Protect", icon: kept ? "lock.open" : "lock", on: kept) {
                            if !library.setProtected(lid, !kept) { localNote = "Unlock the Library to unprotect." }
                        }
                    }
                    if actions.delete != nil {
                        ToolChip(title: "Delete", icon: "trash", on: false) { confirmDelete = true }
                    }
                }
            }
            .padding(.horizontal)
        }
        .disabled(busy != nil)
    }

    private var animateSheet: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("What should move? (optional)", text: $animateWords, axis: .vertical)
                } footer: {
                    Text("About 2–3 minutes on your computer.")
                }
                Section {
                    Button("Make video") {
                        askAnimate = false
                        if let item { actions.animate?(item, animateWords.trimmingCharacters(in: .whitespacesAndNewlines)) }
                        finish()
                    }
                }
            }
            .navigationTitle("Animate")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .topBarLeading) { Button("Cancel") { askAnimate = false } } }
        }
        .presentationDetents([.medium])
    }

    // MARK: actions

    private func togglePainting() {
        painting.toggle()
        if !painting { strokes = [] }
        localNote = nil
    }

    private func removeTapped(_ item: MediaItem) {
        if painting, let mask = currentMask() {
            actions.remove?(item, mask)
            finish()
        } else {
            painting = true
            localNote = "Paint over what to remove, then tap Remove."
        }
    }

    private func submit() {
        guard let item, busy == nil else { return }
        let text = words.trimmingCharacters(in: .whitespacesAndNewlines)
        let mask = currentMask()
        if text.isEmpty {
            guard let mask, let remove = actions.remove else {
                localNote = actions.remove == nil ? "Say what to change." : "Paint the spot first, or say what to change."
                return
            }
            remove(item, mask)
        } else if let change = actions.change {
            change(item, text, mask, MediaChange(mode: changeMode, strength: mask == nil ? nil : amount))
        }
        finish()
    }

    /// After an action: the paint is spent; the chat closes the viewer so its progress shows in the conversation.
    private func finish() {
        typing = false
        words = ""
        strokes = []
        painting = false
        localNote = nil
        if actions.closesAfterAction { dismiss() }
    }

    /// The painted spot as a mask the size of the picture, or nil when nothing (visible) is painted.
    private func currentMask() -> CGImage? {
        guard !strokes.isEmpty, let image,
              let m = BrushMask.render(strokes, size: CGSize(width: image.size.width * image.scale,
                                                             height: image.size.height * image.scale)),
              ImageKit.maskBounds(m) != nil else { return nil }
        return m
    }

    private func isProtected(_ id: UUID) -> Bool {
        library.entries.first { $0.id == id }?.isProtected
            ?? FileManager.default.fileExists(atPath: LibraryVault.file(id, "protected").path)
    }

    private var deleteTitle: String {
        if let lid = item?.libraryID, isProtected(lid) { return "It's protected. Delete anyway?" }
        return "Delete this?"
    }

    private func deleteCurrent() {
        guard let item, let i = items.firstIndex(where: { $0.id == item.id }) else { return }
        if items.count <= 1 {
            actions.delete?(item)
            dismiss()
        } else {
            current = items[i == items.count - 1 ? i - 1 : i + 1].id      // move off it before it goes
            actions.delete?(item)
        }
    }

    private func save() {
        guard let item else { return }
        let picture = loader.images[item.id]
        let video = loader.videos[item.id]
        PHPhotoLibrary.requestAuthorization(for: .addOnly) { status in
            guard status == .authorized || status == .limited else {
                Task { @MainActor in localNote = "Allow Fixer to add photos in iOS Settings to save." }
                return
            }
            PHPhotoLibrary.shared().performChanges({
                if let picture, let data = picture.pngData() {
                    PHAssetCreationRequest.forAsset().addResource(with: .photo, data: data, options: nil)
                } else if let video {
                    PHAssetCreationRequest.forAsset().addResource(with: .video, fileURL: video, options: nil)
                }
            }) { ok, _ in Task { @MainActor in localNote = ok ? "Saved to Photos." : "Couldn't save." } }
        }
    }
}

// MARK: - A video page

/// Keeps one video player and its looper and time observer, so the page can be torn down cleanly.
@MainActor
final class VideoHolder: ObservableObject {
    let player = AVQueuePlayer()
    @Published var position: Double = 0
    @Published var duration: Double = 1
    @Published var playing = false
    private var looper: AVPlayerLooper?
    private var observer: Any?
    private var started = false

    func start(_ url: URL) async {
        guard !started else { return }
        started = true
        looper = AVPlayerLooper(player: player, templateItem: AVPlayerItem(url: url))
        if let d = try? await AVURLAsset(url: url).load(.duration).seconds, d.isFinite, d > 0 { duration = d }
        observer = player.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.1, preferredTimescale: 600),
                                                  queue: .main) { [weak self] t in
            MainActor.assumeIsolated { self?.position = t.seconds }
        }
    }

    func play() { player.play(); playing = true }
    func pause() { player.pause(); playing = false }
    func seek(_ seconds: Double) {
        player.seek(to: CMTime(seconds: seconds, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero)
    }

    func stop() {
        player.pause()
        playing = false
        if let observer { player.removeTimeObserver(observer) }
        observer = nil
        started = false
        looper = nil
    }
}

/// One video in the pager: loops, pinch to zoom, tap to pause, scrub with the slider. Plays only while it is the
/// page on screen.
struct VideoPage: View {
    let url: URL
    let active: Bool
    @StateObject private var holder = VideoHolder()
    @State private var scrubbing = false
    @State private var scrubTo: Double = 0

    var body: some View {
        ZStack(alignment: .bottom) {
            ZoomableVideo(player: holder.player)
                .ignoresSafeArea()
                .onTapGesture { if holder.playing { holder.pause() } else { holder.play() } }
            HStack(spacing: 10) {
                Button { if holder.playing { holder.pause() } else { holder.play() } } label: {
                    Image(systemName: holder.playing ? "pause.fill" : "play.fill").iconButton()
                }
                .accessibilityLabel(holder.playing ? "Pause" : "Play")
                Slider(value: Binding(get: { scrubbing ? scrubTo : holder.position },
                                      set: { scrubTo = $0; holder.seek($0) }),
                       in: 0...max(0.1, holder.duration),
                       onEditingChanged: { scrubbing = $0 })
                    .tint(.white)
            }
            .padding(.horizontal, 12)
            .background(.black.opacity(0.35))
        }
        .task(id: url) {
            await holder.start(url)
            if active { holder.play() }
        }
        .onChange(of: active) { _, on in if on { holder.play() } else { holder.pause() } }
        .onDisappear { holder.stop() }
    }
}

// MARK: - Zoomable picture

/// A picture you can pinch to zoom (up to 8x), drag around while zoomed, and double-tap to zoom in or back out.
struct ZoomableImage: UIViewRepresentable {
    let image: UIImage

    func makeUIView(context: Context) -> UIScrollView {
        let scroll = UIScrollView()
        scroll.delegate = context.coordinator
        scroll.minimumZoomScale = 1
        scroll.maximumZoomScale = 8
        scroll.bouncesZoom = true
        scroll.showsHorizontalScrollIndicator = false
        scroll.showsVerticalScrollIndicator = false
        scroll.backgroundColor = .black
        scroll.contentInsetAdjustmentBehavior = .never
        scroll.isScrollEnabled = false            // 11-21: no panning until zoomed, so the gallery pager snaps cleanly
        let view = UIImageView(image: image)
        view.contentMode = .scaleAspectFit
        view.translatesAutoresizingMaskIntoConstraints = false
        scroll.addSubview(view)
        NSLayoutConstraint.activate([
            view.widthAnchor.constraint(equalTo: scroll.frameLayoutGuide.widthAnchor),
            view.heightAnchor.constraint(equalTo: scroll.frameLayoutGuide.heightAnchor),
            view.leadingAnchor.constraint(equalTo: scroll.contentLayoutGuide.leadingAnchor),
            view.trailingAnchor.constraint(equalTo: scroll.contentLayoutGuide.trailingAnchor),
            view.topAnchor.constraint(equalTo: scroll.contentLayoutGuide.topAnchor),
            view.bottomAnchor.constraint(equalTo: scroll.contentLayoutGuide.bottomAnchor),
        ])
        context.coordinator.imageView = view
        let doubleTap = UITapGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.doubleTapped(_:)))
        doubleTap.numberOfTapsRequired = 2
        scroll.addGestureRecognizer(doubleTap)
        return scroll
    }

    func updateUIView(_ scroll: UIScrollView, context: Context) {
        if context.coordinator.imageView?.image !== image {
            context.coordinator.imageView?.image = image
            scroll.setZoomScale(1, animated: false)
            scroll.isScrollEnabled = false        // back to a fresh image at 1x: let the pager own the swipe again
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    final class Coordinator: NSObject, UIScrollViewDelegate {
        weak var imageView: UIImageView?

        func viewForZooming(in scrollView: UIScrollView) -> UIView? { imageView }

        // 11-21: allow panning only while zoomed in; at 1x the TabView pager gets the swipe and snaps cleanly.
        func scrollViewDidZoom(_ scrollView: UIScrollView) {
            scrollView.isScrollEnabled = scrollView.zoomScale > scrollView.minimumZoomScale
        }

        @objc func doubleTapped(_ gesture: UITapGestureRecognizer) {
            guard let scroll = gesture.view as? UIScrollView, let imageView else { return }
            if scroll.zoomScale > 1.01 {
                scroll.setZoomScale(1, animated: true)
            } else {
                let point = gesture.location(in: imageView)
                let size = CGSize(width: scroll.bounds.width / 3, height: scroll.bounds.height / 3)
                scroll.zoom(to: CGRect(x: point.x - size.width / 2, y: point.y - size.height / 2,
                                       width: size.width, height: size.height), animated: true)
            }
        }
    }
}
