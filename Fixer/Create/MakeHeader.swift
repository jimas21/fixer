import SwiftUI

/// The controls that float over the picture: the queue, Picture / Video, the Explicit switch and Stop all.
extension CreateScreen {
    var makeHeader: some View {
        HStack(spacing: 6) {
            if desktop.isOn {
                Button { showQueue = true } label: {
                    Image(systemName: "list.number")
                        .font(.system(size: 20, weight: .regular))
                        .foregroundStyle(.primary)
                        .frame(width: 44, height: 44)
                        .background(Circle().fill(Fx.bar))
                        .overlay(Circle().strokeBorder(Fx.hairline, lineWidth: 0.5))
                        .contentShape(Circle())
                }
                .accessibilityLabel("Open the queue")
                .accessibilityHint("See what's running on your computer")
            }
            MakeModeSwitch(mode: $mode)
            explicitSwitch
            Spacer(minLength: 0)
            Button(action: stopEverything) {
                Text("Stop all")
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(Fx.danger)
                    .lineLimit(1)
                    .padding(.horizontal, 8)
                    .frame(minWidth: 44, minHeight: 44)
                    .contentShape(Rectangle())
            }
            .opacity(showStopAll ? 1 : 0)
            .disabled(!showStopAll)
            .accessibilityHidden(!showStopAll)
            .accessibilityLabel("Stop everything")
            .accessibilityHint("Stops every picture and video this iPhone has running on your computer")
        }
        .padding(12)
        .dynamicTypeSize(...DynamicTypeSize.xxLarge)
        .fxAnimation(Motion.pop, value: showStopAll)
    }

    /// Swaps Make to the explicit recipes, models and quick picks (and back). Off never blocks or rewrites anything.
    var explicitSwitch: some View {
        Button { makeExplicit.toggle() } label: {
            HStack(spacing: 6) {
                Text("Explicit").font(.footnote.weight(.semibold)).lineLimit(1)
                ZStack(alignment: makeExplicit ? .trailing : .leading) {
                    Capsule().fill(makeExplicit ? Fx.explicitStroke : Fx.switchTrack).frame(width: 30, height: 18)
                    Circle().fill(Color.white).frame(width: 14, height: 14).padding(2)
                }
            }
            .foregroundStyle(makeExplicit ? Fx.explicitText : Color.primary)
            .padding(.horizontal, 10)
            .frame(height: 44)
            .background(Capsule().fill(makeExplicit ? Fx.explicitSwitch : Fx.bar))
            .overlay(Capsule().strokeBorder(makeExplicit ? Fx.explicitStroke : Fx.hairline, lineWidth: makeExplicit ? 1 : 0.5))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .fxAnimation(Motion.pop, value: makeExplicit)
        .accessibilityLabel("Explicit recipes")
        .accessibilityValue(makeExplicit ? "On" : "Off")
        .accessibilityAddTraits(.isToggle)
    }
}

/// Picture | Video, a two-part capsule.
struct MakeModeSwitch: View {
    @Binding var mode: String

    var body: some View {
        HStack(spacing: 0) {
            segment("Picture", "picture")
            segment("Video", "video")
        }
        .padding(3)
        .frame(width: 118, height: 44)
        .background(Capsule().fill(Fx.bar))
        .overlay(Capsule().strokeBorder(Fx.hairline, lineWidth: 0.5))
        .fxAnimation(Motion.pop, value: mode)
        .accessibilityRepresentation {
            Picker("Make", selection: $mode) {
                Text("Picture").tag("picture")
                Text("Video").tag("video")
            }
            .pickerStyle(.segmented)
        }
    }

    private func segment(_ title: String, _ value: String) -> some View {
        Button { mode = value } label: {
            Text(title)
                .font(.footnote.weight(.semibold))
                .foregroundStyle(mode == value ? Color.primary : Fx.secondaryText)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background { if mode == value { Capsule().fill(Fx.segment) } }
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
    }
}
