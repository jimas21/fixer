import PhotosUI
import SwiftUI

/// Make as one compose sheet (owner's pick, 11-18: "one compose sheet, show the pic at the top, click it to get
/// bigger, the roll below it"). The picture sits in a card at the top — tap it to open full screen — the roll of this
/// session's pictures sits under it, and then every control is inline in a single scroll: recipe chips, the words,
/// quick picks, the tiles, the photo, style tips, add-ons and the model's settings. Nothing is hidden behind a drawer.
/// The one wide Make button is pinned at the bottom (it rides above the keyboard when the words have it).
extension CreateScreen {
    @ViewBuilder var makeComposeStage: some View {
        if mode == "song" {
            VStack(spacing: 0) {                         // 11-20: songs have their own form (SongMaker.swift)
                makeHeader
                SongMakerView()
            }
        } else {
            pictureComposeStage
        }
    }

    private var pictureComposeStage: some View {
        VStack(spacing: 0) {
            makeHeader
            ScrollView {
                VStack(spacing: 12) {
                    ForEach(composeStack) { section in
                        section.view
                    }
                }
                .padding(.horizontal, 12)
                .padding(.top, 4)
                .padding(.bottom, 16)
                // 11-19: pinned to exactly the screen's width. Anything inside wider than that (the chip strips run to
                // the card edge with a negative padding) made this vertical scroll pan sideways too, so the owner could
                // grab the whole Make page and drag it around.
                .containerRelativeFrame(.horizontal)
                .clipped()
            }
            .scrollDismissesKeyboard(.interactively)
            .scrollIndicators(.hidden)
            .scrollBounceBehavior(.basedOnSize, axes: .horizontal)
        }
        .safeAreaInset(edge: .bottom) { composeBottomBar }
    }

    // MARK: the picture at the top

    /// A bounded card with the picture (the live preview while one is drawn, else the one being worked on). Tap it to
    /// open it full screen. On a short screen it still keeps the controls reachable because everything scrolls.
    var composePicture: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 20, style: .continuous).fill(Fx.tile)
            composePictureInner
            composePictureOverlay
        }
        .frame(height: 300)
        .frame(maxWidth: .infinity)
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 20, style: .continuous).strokeBorder(Fx.hairline, lineWidth: 0.5))
    }

    @ViewBuilder var composePictureInner: some View {
        if createRunning, let preview {
            composeFit(preview, badge: false, tap: nil)
        } else if changeRunning, let w = working, store.reviseBaseID == w.id {
            composeFit(store.revisePreview ?? w.image, badge: false, tap: nil)
        } else if let w = working {
            composeFit(w.image, badge: w.videoURL != nil) { viewing = w }
        } else {
            canvasEmpty
        }
    }

    func composeFit(_ image: UIImage, badge: Bool, tap: (() -> Void)?) -> some View {
        Image(uiImage: image)
            .resizable()
            .scaledToFit()
            .overlay {
                if badge {
                    Image(systemName: "play.circle.fill").font(.system(size: 44)).foregroundStyle(.white).shadow(radius: 3)
                }
            }
            .overlay(alignment: .bottomTrailing) {
                if tap != nil {
                    Image(systemName: "arrow.up.left.and.arrow.down.right")
                        .font(.system(size: 12, weight: .bold))
                        .foregroundStyle(.white)
                        .frame(width: 30, height: 30)
                        .background(Circle().fill(.black.opacity(0.45)))
                        .padding(8)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .contentShape(Rectangle())
            .onTapGesture {
                if focus != nil || keyboard.visible { focus = nil; Self.hideKeyboard() } else { tap?() }
            }
            .accessibilityElement()
            .accessibilityLabel(badge ? "Video, open full screen" : "Picture, open full screen")
            .accessibilityAddTraits(.isButton)
    }

    /// What the computer is doing (a pill), or the last picture's "Changed: …" note, over the picture card.
    @ViewBuilder var composePictureOverlay: some View {
        if let text = runStatus {
            VStack {
                Spacer()
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text(text).font(.footnote.weight(.semibold)).lineLimit(2)
                }
                .padding(.horizontal, 12).padding(.vertical, 7)
                .background(Capsule().fill(Fx.barSoft))
                .overlay(Capsule().strokeBorder(Fx.hairline, lineWidth: 0.5))
                .padding(.bottom, 10)
            }
            .allowsHitTesting(false)
        } else if !createRunning, !changeRunning, let note = working?.note {
            VStack {
                Spacer()
                Text(note)
                    .font(.footnote).foregroundStyle(.white.opacity(0.9)).lineLimit(2)
                    .multilineTextAlignment(.center).shadow(radius: 3)
                    .padding(.horizontal, 16).padding(.bottom, 10)
            }
            .allowsHitTesting(false)
        }
    }

    // MARK: the controls, all inline

    /// The whole scroll as a FLAT list of type-erased sections (the picture, the roll, then the controls). Erasing
    /// each one to AnyView is what keeps this safe: stacking every control inline as one combined view made the
    /// scroll content's static generic type so deeply nested that resolving its metadata overflowed the stack the
    /// instant Make appeared (SIGSEGV in swift_getTypeByMangledName). One AnyView per section stops the types fusing.
    ///
    /// 11-22: each section has its own steady id, so a section that appears or goes (the roll, the trigger words, the
    /// Body panel) no longer shifts every later one: they keep their sheets and open/closed state.
    var composeStack: [MakeSection] {
        var out: [MakeSection] = [MakeSection("picture", AnyView(composePicture))]
        // 11-19 (the owner): the model / shape / pictures / enhance tiles sit right under the picture, before any
        // other setting, in every mode.
        if let tiles = composeTiles { out.append(MakeSection("tiles", tiles)) }
        if store.results.count > 1 { out.append(MakeSection("roll", AnyView(resultsRoll))) }
        out.append(contentsOf: composeSections)
        return out
    }

    /// The tile row for what is being made (pinned under the picture by `composeStack`).
    var composeTiles: AnyView? {
        if effectiveTarget == .change { return AnyView(changeTiles) }
        if mode == "video" { return AnyView(videoTiles) }
        if let r = recipe { return AnyView(pictureTiles(r)) }
        return nil
    }

    /// The controls for what is being made, each its own card, each type-erased (see `composeStack`).
    var composeSections: [MakeSection] {
        var out: [MakeSection] = []
        if hasRecipeRow { out.append(MakeSection("recipes", AnyView(recipeRow))) }
        out.append(MakeSection("prompt", AnyView(promptBox)))
        if effectiveTarget == .change {
            if hasQuickPicks { out.append(MakeSection("quick", AnyView(quickSectionsCard(tips: [])))) }
            out.append(MakeSection("help", AnyView(helpPage)))
        } else if mode == "video" {
            if !activeTriggerWords.isEmpty { out.append(MakeSection("triggers", AnyView(triggerRow))) }
            out.append(MakeSection("videoPhoto", AnyView(videoPhotoPage)))
            out.append(MakeSection("addons", AnyView(drawerSection("Add-ons") { loraSection })))
            out.append(MakeSection("videoSettings", AnyView(videoSettingsPage)))
        } else if let r = recipe {
            out.append(MakeSection("macros", AnyView(macrosCard)))               // 11-21: saved macros, right in Make
            if !activeTriggerWords.isEmpty { out.append(MakeSection("triggers", AnyView(triggerRow))) }   // 11-21: LoRA trigger words, tap to leave out
            let tips = shownToggles(r)              // 11-19: style tips sit inside the quick-pick card now
            if hasQuickPicks || !tips.isEmpty { out.append(MakeSection("quick", AnyView(quickSectionsCard(tips: tips)))) }
            if showBodyPanel { out.append(MakeSection("body", AnyView(drawerSection("Body") { bodyPanel }))) }
            out.append(MakeSection("photo", AnyView(drawerSection("Photo") { photoSection })))
            out.append(MakeSection("addons", AnyView(drawerSection("Add-ons") { loraSection })))
            out.append(MakeSection("settings", AnyView(pictureSettingsPage(r))))
        }
        return out
    }

    var hasQuickPicks: Bool { recipeStore.catalog != nil && !activeGroups.isEmpty }


    // MARK: the pinned button

    var composeBottomBar: some View {
        VStack(spacing: 0) {
            mainButton
        }
        .padding(.horizontal, 12)
        .padding(.top, 8)
        .padding(.bottom, 8)
        .background(.bar)
    }
}

/// One section of Make's scroll, with a steady id (see `composeStack`).
struct MakeSection: Identifiable {
    let id: String
    let view: AnyView
    init(_ id: String, _ view: AnyView) { self.id = id; self.view = view }
}
