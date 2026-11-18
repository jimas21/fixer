import SwiftUI
import UIKit

/// The full-bleed picture area of Make: this session's pictures (swipe, or tap one in the roll under the picture),
/// the live preview while one is drawn, and a pill with what the computer is doing. The picture is always fitted into
/// the room above the panel, whatever the panel is showing, so the panel never sits on top of it.
extension CreateScreen {
    /// The room the roll of this session's pictures takes under the picture.
    static let rollHeight: CGFloat = 56

    var makeCanvas: some View {
        GeometryReader { full in
            let top = full.safeAreaInsets.top + 64
            let roll = showRoll
            // Everything below the panel's bottom edge (the tab bar and home indicator, or the keyboard), then the panel.
            let under = panelHeight + Swift.max(full.safeAreaInsets.bottom, keyboard.visible ? keyboard.height : 0)
            // Where the picture's room ends. On a very short screen it keeps 120 pt rather than vanish.
            let ground = Swift.min(under + (roll ? Self.rollHeight : 0) + 8, Swift.max(0, full.size.height - top - 120))
            ZStack {
                canvasBackdrop(full.size)
                canvasForeground
                    .padding(.top, top)
                    .padding(.bottom, ground)
                    .frame(width: full.size.width, height: full.size.height)
                canvasNote
                    .padding(.top, top)
                    .frame(width: full.size.width, height: full.size.height, alignment: .top)
                    .allowsHitTesting(false)
                if roll {
                    resultsRoll
                        .padding(.bottom, under + 4)
                        .frame(width: full.size.width, height: full.size.height, alignment: .bottom)
                        .transition(.opacity)
                }
                progressPill
                    .padding(.bottom, ground + 10)
                    .frame(width: full.size.width, height: full.size.height, alignment: .bottom)
            }
            .frame(width: full.size.width, height: full.size.height)
            .fxAnimation(Motion.snap, value: ground)
            .fxAnimation(Motion.snap, value: roll)
        }
        .ignoresSafeArea()
    }

    /// The picture on the canvas right now (the live preview, the picture being changed, or the one picked).
    var canvasImage: UIImage? {
        if createRunning, let preview { return preview }
        if changeRunning, let w = working, store.reviseBaseID == w.id { return store.revisePreview ?? w.image }
        return working?.image
    }

    /// A blurred, darkened copy fills the edges so the picture runs edge to edge without being cropped.
    func canvasBackdrop(_ size: CGSize) -> some View {
        ZStack {
            Fx.canvas
            if let img = canvasImage {
                Image(uiImage: img)
                    .resizable()
                    .scaledToFill()
                    .frame(width: size.width, height: size.height)
                    .blur(radius: 40)
                    .overlay(Color.black.opacity(0.6))
                    .transition(.opacity)
            }
        }
        .frame(width: size.width, height: size.height)
        .clipped()
    }

    @ViewBuilder var canvasForeground: some View {
        if createRunning, let preview {
            fitPicture(preview, badge: false) { }
        } else if changeRunning, let w = working, store.reviseBaseID == w.id {
            fitPicture(store.revisePreview ?? w.image, badge: false) { }
        } else if !store.results.isEmpty {
            if changeRunning, let w = working {
                // A change is running on another picture: the one picked in the roll just shows; swiping waits.
                fitPicture(w.image, badge: w.videoURL != nil) { viewing = w }
            } else {
                TabView(selection: workingSelection) {
                    ForEach(store.results) { r in
                        fitPicture(r.image, badge: r.videoURL != nil) { viewing = r }
                            .tag(Optional(r.id))
                    }
                }
                .tabViewStyle(.page(indexDisplayMode: .never))
            }
        } else {
            canvasEmpty
        }
    }

    var workingSelection: Binding<UUID?> {
        Binding(get: { working?.id }, set: { if let id = $0 { workingID = id } })
    }

    /// A tap on the canvas first puts away whatever is in the way (the keyboard, then the open settings); only a tap
    /// on a clear canvas does `then`.
    func canvasTapped(_ then: () -> Void) {
        if focus != nil || keyboard.visible {
            focus = nil
            Self.hideKeyboard()
        } else if drawerShowing {
            setPanel(open: false)
        } else {
            then()
        }
    }

    func fitPicture(_ image: UIImage, badge: Bool, tap: @escaping () -> Void) -> some View {
        Image(uiImage: image)
            .resizable()
            .scaledToFit()
            .overlay {
                if badge {
                    Image(systemName: "play.circle.fill").font(.system(size: 44)).foregroundStyle(.white).shadow(radius: 3)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .contentShape(Rectangle())
            .onTapGesture { canvasTapped(tap) }
            .accessibilityElement()
            .accessibilityLabel(badge ? "Video, open full screen" : "Picture, open full screen")
            .accessibilityAddTraits(.isButton)
    }

    @ViewBuilder var canvasEmpty: some View {
        VStack(spacing: 10) {
            if mode == "picture", loading {
                ProgressView()
                Text("Asking your computer…").foregroundStyle(Fx.secondaryText)
            } else if mode == "picture", let failed {
                Text(failed).foregroundStyle(Fx.secondaryText).multilineTextAlignment(.center)
                Button("Try again") { Task { await load() } }
                    .buttonStyle(.bordered)
                    .frame(minHeight: 44)
            } else if mode == "picture", recipes.isEmpty {
                Text("No picture models are on your computer yet.")
                    .foregroundStyle(Fx.secondaryText).multilineTextAlignment(.center)
            } else {
                Image(systemName: mode == "video" ? "film" : "photo")
                    .font(.system(size: 36)).foregroundStyle(Fx.secondaryText)
                Text(mode == "video" ? "Your video shows here" : "Your picture shows here")
                    .foregroundStyle(Fx.secondaryText)
            }
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .contentShape(Rectangle())
        .onTapGesture { canvasTapped { } }
    }

    /// "Changed: add a hat" for the picture on the canvas, when it has such a note.
    @ViewBuilder var canvasNote: some View {
        if !createRunning, !changeRunning, let note = working?.note {
            Text(note)
                .font(.footnote)
                .foregroundStyle(.white.opacity(0.85))
                .lineLimit(2)
                .multilineTextAlignment(.center)
                .shadow(radius: 3)
                .padding(.horizontal, 24)
        }
    }

    // MARK: the roll

    /// The roll shows whenever there is more than one picture and nothing else needs the room.
    var showRoll: Bool { store.results.count > 1 && !drawerShowing && !keyboard.visible }

    /// Everything made this session as a strip of thumbnails under the picture, oldest on the left like the swipe
    /// order. Tap one to put it on the canvas; tap the one that is showing to open it full screen. The strip keeps
    /// the one on the canvas in the middle, and ends with a way into the Library.
    var resultsRoll: some View {
        let list = store.results
        let current = working?.id
        return ScrollViewReader { proxy in
            ScrollView(.horizontal, showsIndicators: false) {
                LazyHStack(spacing: 6) {
                    ForEach(Array(list.enumerated()), id: \.element.id) { index, r in
                        rollThumb(r, index: index, count: list.count, on: r.id == current)
                            .id(r.id)
                    }
                    Button { router.tab = .library } label: {
                        Image(systemName: "photo.stack")
                            .font(.system(size: 17))
                            .foregroundStyle(.white.opacity(0.9))
                            .frame(width: 44, height: 44)
                            .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.white.opacity(0.14)))
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("All in Library")
                }
                .padding(.horizontal, 14)
            }
            .frame(height: 48)
            .onChange(of: current) { _, id in
                guard let id else { return }
                withAnimation(Motion.snap) { proxy.scrollTo(id, anchor: .center) }
            }
            .onAppear {
                if let current { proxy.scrollTo(current, anchor: .center) }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Made this session")
    }

    private func rollThumb(_ r: CreateResult, index: Int, count: Int, on: Bool) -> some View {
        Button {
            if on {
                viewing = r
            } else {
                withAnimation(Motion.snap) { workingID = r.id }
            }
        } label: {
            Image(uiImage: r.image)
                .resizable()
                .scaledToFill()
                .frame(width: 44, height: 44)
                .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                .overlay {
                    if r.videoURL != nil {
                        Image(systemName: "play.fill")
                            .font(.system(size: 13, weight: .bold))
                            .foregroundStyle(.white)
                            .shadow(radius: 2)
                    }
                }
                .overlay {
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .strokeBorder(Color.white.opacity(on ? 1 : 0.18), lineWidth: on ? 2 : 0.5)
                }
                .opacity(on ? 1 : 0.7)
                .scaleEffect(on ? 1 : 0.9)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .contextMenu {
            if r.videoURL == nil {
                Button { useForVideo(r) } label: { Label("Make a video from it", systemImage: "film") }
            }
            Button { viewing = r } label: { Label("Open full screen", systemImage: "arrow.up.left.and.arrow.down.right") }
        }
        .fxAnimation(Motion.pop, value: on)
        .accessibilityLabel("\(r.videoURL != nil ? "Video" : "Picture") \(index + 1) of \(count)")
        .accessibilityHint(on ? "Opens it full screen" : "Shows it on the canvas")
        .accessibilityAddTraits(on ? .isSelected : [])
    }

    // MARK: the pill

    /// "Drawing · step 18 of 28 · about 40 s" while a run goes (the computer words it); the last message when idle.
    @ViewBuilder var progressPill: some View {
        if let text = runStatus {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(text).font(.footnote.weight(.semibold)).lineLimit(2)
                Button(action: stopWhatRuns) {
                    Image(systemName: "xmark.circle.fill").font(.system(size: 18)).foregroundStyle(Fx.secondaryText)
                        .frame(width: 44, height: 44).contentShape(Rectangle())
                }
                .accessibilityLabel("Stop this")
            }
            .padding(.leading, 14).padding(.trailing, 2)
            .frame(minHeight: 34)
            .background(Capsule().fill(Fx.barSoft))
            .overlay(Capsule().strokeBorder(Fx.hairline, lineWidth: 0.5))
            .padding(.horizontal, 16)
            .transition(.opacity.combined(with: .scale(scale: 0.96)))
            .fxAnimation(Motion.pop, value: text)
        } else if let note = idleNote {
            Button { noteDismissed = note } label: {
                Text(note)
                    .font(.footnote.weight(.semibold))
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 14).padding(.vertical, 8)
                    .frame(minHeight: 34)
                    .background(Capsule().fill(Fx.barSoft))
                    .overlay(Capsule().strokeBorder(Fx.hairline, lineWidth: 0.5))
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 16)
            .transition(.opacity.combined(with: .scale(scale: 0.96)))
            .accessibilityHint("Tap to dismiss")
            .sensoryFeedback(.warning, trigger: note)
        }
    }
}
