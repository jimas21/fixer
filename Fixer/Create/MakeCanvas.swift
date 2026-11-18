import SwiftUI
import UIKit

/// The full-bleed picture area of Make: this session's pictures (swipe), the live preview while one is drawn, and a
/// pill with what the computer is doing. Everything here is an overlay or a fixed frame, so nothing jumps when a run
/// starts or ends, and it never resizes for the keyboard or the open panel.
extension CreateScreen {
    var makeCanvas: some View {
        GeometryReader { full in
            ZStack {
                canvasBackdrop(full.size)
                canvasForeground
                    .padding(.top, full.safeAreaInsets.top + 64)
                    .padding(.bottom, restingPanelHeight + 8 + full.safeAreaInsets.bottom)
                    .frame(width: full.size.width, height: full.size.height)
                    .fxAnimation(Motion.snap, value: restingPanelHeight)
                VStack(spacing: 6) {
                    if store.results.count > 1, !changeRunning, let w = working {
                        let list = store.results
                        let index = list.firstIndex(where: { $0.id == w.id }) ?? list.count - 1
                        Text("\(index + 1) of \(list.count)")
                            .font(.caption.weight(.semibold))
                            .padding(.horizontal, 12).frame(minHeight: 28)
                            .background(Capsule().fill(Fx.barSoft))
                            .overlay(Capsule().strokeBorder(Fx.hairline, lineWidth: 0.5))
                        if let note = w.note {
                            Text(note)
                                .font(.footnote)
                                .foregroundStyle(.white.opacity(0.85))
                                .lineLimit(2)
                                .multilineTextAlignment(.center)
                                .shadow(radius: 3)
                                .padding(.horizontal, 24)
                        }
                    }
                    Spacer(minLength: 0)
                }
                .padding(.top, full.safeAreaInsets.top + 64)
                .frame(width: full.size.width, height: full.size.height)
                .allowsHitTesting(false)
                progressPill
                    .padding(.bottom, restingPanelHeight + 8 + full.safeAreaInsets.bottom + 12)
                    .frame(width: full.size.width, height: full.size.height, alignment: .bottom)
                    .fxAnimation(Motion.snap, value: restingPanelHeight)
            }
            .frame(width: full.size.width, height: full.size.height)
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
            if changeRunning {
                canvasEmpty              // a change on a picture that is not on the canvas: nothing to flip through
            } else {
                TabView(selection: workingSelection) {
                    ForEach(store.results) { r in
                        fitPicture(r.image, badge: r.videoURL != nil) { openResult(r) }
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

    func openResult(_ r: CreateResult) {
        if focus != nil {
            focus = nil
            Self.hideKeyboard()
        } else {
            viewing = r
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
            .onTapGesture {
                if focus != nil { focus = nil; Self.hideKeyboard() } else { tap() }
            }
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
        .onTapGesture { if focus != nil { focus = nil; Self.hideKeyboard() } }
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
