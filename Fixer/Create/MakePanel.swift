import SwiftUI

/// The panel under the picture. Closed, it is the dock: the prompt, the tiles and Create. Open, a drawer of pages
/// (quick picks, photo, style tips, add-ons, settings) sits above a shorter dock, one tap per page, and the picture
/// shrinks to stay in view above it. It is an overlay, not a sheet: a sheet would cover the tab bar, fight the
/// keyboard and need a second presentation for every list opened from it.
extension CreateScreen {
    func makePanel(containerHeight: CGFloat) -> some View {
        let typing = isTyping
        let open = drawerShowing
        return VStack(spacing: 0) {
            panelHandle
            if open {
                makeDrawer(maxHeight: drawerCap(containerHeight))
                    .transition(.opacity)
            }
            makeDock(typing: typing, open: open)
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { dockHeight = $0 }
        }
        .padding(.horizontal, 12)
        .padding(.bottom, 12)
        .glass(radius: Fx.panelRadius, tint: Fx.panel)
        .padding(.horizontal, 8)
        .padding(.bottom, 8)
        .offset(y: panelOffset)
        .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { h in
            if h > 0 { panelHeight = h }            // always measured: the canvas fits the picture above whatever shows
        }
        .fxAnimation(Motion.snap, value: open)
        .fxAnimation(Motion.snap, value: typing)
        .fxAnimation(Motion.snap, value: section)
        .fxAnimation(Motion.follow, value: panelDrag)
        .sensoryFeedback(.selection, trigger: panelOpen)
        .sensoryFeedback(.selection, trigger: savedSection)
    }

    /// The most height the open page may take, so the picture above always keeps a strip of its own.
    func drawerCap(_ containerHeight: CGFloat) -> CGFloat {
        let picture: CGFloat = keyboard.visible ? 0 : 150
        let chrome: CGFloat = 64 + 30 + 52 + 36          // the header, the handle, the page tabs, paddings and gaps
        return Swift.max(120, containerHeight - picture - chrome - dockHeight)
    }

    /// While the handle is dragged the panel follows a little: down when there is something to put away, barely
    /// otherwise, and hardly at all upwards (it opens when the finger lifts).
    private var panelOffset: CGFloat {
        if panelDrag > 0 {
            return drawerShowing || keyboard.visible ? Swift.min(panelDrag * 0.5, 60) : Swift.min(panelDrag * 0.2, 14)
        }
        return Swift.max(panelDrag * 0.2, -14)
    }

    /// The grabber: tap it or flick it up to open the settings, tap or flick down to close them. The thin progress
    /// line lives in the same strip, so a run starting never changes the panel's height.
    var panelHandle: some View {
        Button {
            setPanel(open: !drawerShowing)
        } label: {
            VStack(spacing: 7) {
                Capsule().fill(Fx.grabber).frame(width: 36, height: 5)
                panelProgressBar
            }
            .padding(.top, 8)
            .padding(.bottom, 6)
            .frame(maxWidth: .infinity)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .simultaneousGesture(
            DragGesture(minimumDistance: 6)
                .updating($panelDrag) { value, state, _ in state = value.translation.height }
                .onEnded { value in
                    let predicted = value.predictedEndTranslation.height
                    if predicted < -60 {
                        setPanel(open: true)
                    } else if predicted > 60 {
                        if keyboard.visible {
                            focus = nil
                            Self.hideKeyboard()
                        } else {
                            setPanel(open: false)
                        }
                    }
                })
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("More settings")
        .accessibilityValue(drawerShowing ? "Open, \(section.title)" : "Closed")
        .accessibilityHint("Photo, style tips, add-ons and advanced settings")
        .accessibilityAdjustableAction { direction in
            switch direction {
            case .increment: setPanel(open: true)
            case .decrement: setPanel(open: false)
            @unknown default: break
            }
        }
    }

    /// Always 4 pt tall, so the panel never changes height when a run starts.
    var panelProgressBar: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.primary.opacity(0.12))
                Capsule().fill(Fx.accent).frame(width: g.size.width * CGFloat(Swift.min(1, Swift.max(0, runProgress))))
            }
        }
        .frame(height: 4)
        .padding(.horizontal, 4)
        .opacity(anyRunning ? 1 : 0)
        .accessibilityHidden(true)
    }

    /// The round button beside Create: the same thing as the handle, where a thumb already is.
    var panelToggle: some View {
        let open = drawerShowing
        return Button {
            setPanel(open: !open)
        } label: {
            Image(systemName: "chevron.up")
                .font(.system(size: 17, weight: .semibold))
                .rotationEffect(.degrees(open ? 180 : 0))
                .foregroundStyle(Color.primary)
                .frame(width: 50, height: 50)
                .background(Circle().fill(Fx.solidSoft))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(open ? "Close the settings" : "More settings")
        .accessibilityHint(open ? "" : "Photo, style tips, add-ons and advanced settings")
    }
}
