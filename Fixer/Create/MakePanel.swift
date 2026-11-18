import SwiftUI

/// The settings panel under the picture. Three heights: closed (prompt, tiles, Create), half open (adds quick picks
/// and Age) and open (adds everything else Make offers). It is an overlay, not a sheet: a sheet would cover the tab
/// bar, fight the keyboard and need a second presentation for every list opened from it.
extension CreateScreen {
    func makePanel(containerHeight: CGFloat) -> some View {
        let typing = isTyping
        let open = detent == .large && !typing
        let drawerHeight = Swift.max(160, containerHeight - 44 - 16 - dockHeight - 48 - 70)
        return VStack(spacing: 0) {
            panelGrabber
            panelProgressBar
            if open {
                makeDrawer
                    .frame(height: drawerHeight)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
            makeDock(typing: typing)
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { dockHeight = $0 }
        }
        .padding(.horizontal, 12)
        .padding(.bottom, 12)
        .glass(radius: Fx.panelRadius, tint: Fx.panel)
        .padding(.horizontal, 8)
        .padding(.bottom, 8)
        .offset(y: panelDrag > 0 ? panelDrag : Swift.max(panelDrag * 0.2, -14))
        .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { h in
            if detent == .collapsed, !isTyping, h > 0 { restingPanelHeight = h }
        }
        .fxAnimation(Motion.snap, value: detent)
        .fxAnimation(Motion.snap, value: typing)
        .fxAnimation(Motion.follow, value: panelDrag)
        .sensoryFeedback(.selection, trigger: savedDetent)
    }

    var panelGrabber: some View {
        Button {
            setDetent(detent == .collapsed ? .mid : detent == .mid ? .large : .mid)
        } label: {
            VStack(spacing: 6) {
                Capsule().fill(Fx.grabber).frame(width: 36, height: 5)
                Text(detent == .large ? "Pull down for less" : "Pull up for photo, style tips, add-ons, advanced")
                    .font(.caption)
                    .foregroundStyle(Fx.secondaryText)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
            }
            .frame(maxWidth: .infinity, minHeight: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .simultaneousGesture(
            DragGesture(minimumDistance: 6)
                .updating($panelDrag) { value, state, _ in state = value.translation.height }
                .onEnded { value in
                    let predicted = value.predictedEndTranslation.height
                    if predicted < -260 { setDetent(detent.up.up) }
                    else if predicted < -60 { setDetent(detent.up) }
                    else if predicted > 60 {
                        if focus != nil { focus = nil; Self.hideKeyboard() }
                        setDetent(detent.down)
                    }
                })
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Picture settings")
        .accessibilityValue(detent.title)
        .accessibilityHint("Swipe up or down to open or close the settings")
        .accessibilityAdjustableAction { direction in
            switch direction {
            case .increment: setDetent(detent.up)
            case .decrement: setDetent(detent.down)
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
        .padding(.bottom, 10)
        .opacity(anyRunning ? 1 : 0)
        .accessibilityHidden(true)
    }
}
