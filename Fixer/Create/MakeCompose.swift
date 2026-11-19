import PhotosUI
import SwiftUI

/// Make as one compose sheet (owner's pick, 11-18: "one compose sheet, show the pic at the top, click it to get
/// bigger, the roll below it"). The picture sits in a card at the top — tap it to open full screen — the roll of this
/// session's pictures sits under it, and then every control is inline in a single scroll: recipe chips, the words,
/// quick picks, the tiles, the photo, style tips, add-ons and the model's settings. Nothing is hidden behind a drawer.
/// The one wide Make button is pinned at the bottom (it rides above the keyboard when the words have it).
extension CreateScreen {
    var makeComposeStage: some View {
        VStack(spacing: 0) {
            makeHeader
            ScrollView {
                VStack(spacing: 12) {
                    ForEach(Array(composeStack.enumerated()), id: \.offset) { _, section in
                        section
                    }
                }
                .padding(.horizontal, 12)
                .padding(.top, 4)
                .padding(.bottom, 16)
            }
            .scrollDismissesKeyboard(.interactively)
            .scrollIndicators(.hidden)
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
    var composeStack: [AnyView] {
        var out: [AnyView] = [AnyView(composePicture)]
        if store.results.count > 1 { out.append(AnyView(resultsRoll)) }
        out.append(contentsOf: composeSections)
        return out
    }

    /// The controls for what is being made, each its own card, each type-erased (see `composeStack`).
    var composeSections: [AnyView] {
        var out: [AnyView] = []
        if hasRecipeRow { out.append(AnyView(recipeRow)) }
        out.append(AnyView(promptBox))
        if effectiveTarget == .change {
            out.append(AnyView(changeTiles))
            if hasQuickPicks { out.append(AnyView(quickSectionsCard(tips: []))) }
            out.append(AnyView(helpPage))
        } else if mode == "video" {
            out.append(AnyView(videoTiles))
            out.append(AnyView(videoPhotoPage))
            out.append(AnyView(drawerSection("Add-ons") { loraSection }))
            out.append(AnyView(videoSettingsPage))
        } else if let r = recipe {
            let tips = shownToggles(r)              // 11-19: style tips sit inside the quick-pick card now
            if hasQuickPicks || !tips.isEmpty { out.append(AnyView(quickSectionsCard(tips: tips))) }
            if showBodyPanel { out.append(AnyView(drawerSection("Body") { bodyPanel })) }
            out.append(AnyView(pictureTiles(r)))
            out.append(AnyView(drawerSection("Photo") { photoSection }))
            out.append(AnyView(drawerSection("Add-ons") { loraSection }))
            out.append(AnyView(pictureSettingsPage(r)))
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
