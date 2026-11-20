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
            AdultChip(onBar: true)                       // 11-19: this session's Explicit switch
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
            TidyMenu(chat: chat, onClear: { router.clearedCount += 1 }) { TidyCircleLabel(large: true) }
        }
        .padding(12)
        .dynamicTypeSize(...DynamicTypeSize.xxLarge)
        .fxAnimation(Motion.pop, value: showStopAll)
    }

}

/// Picture | Video | Song, a three-part capsule (Song: 11-20).
struct MakeModeSwitch: View {
    @Binding var mode: String

    var body: some View {
        HStack(spacing: 0) {
            segment("Picture", "picture")
            segment("Video", "video")
            segment("Song", "song")
        }
        .padding(3)
        .frame(width: 166, height: 44)
        .background(Capsule().fill(Fx.bar))
        .overlay(Capsule().strokeBorder(Fx.hairline, lineWidth: 0.5))
        .fxAnimation(Motion.pop, value: mode)
        .accessibilityRepresentation {
            Picker("Make", selection: $mode) {
                Text("Picture").tag("picture")
                Text("Video").tag("video")
                Text("Song").tag("song")
            }
            .pickerStyle(.segmented)
        }
    }

    private func segment(_ title: String, _ value: String) -> some View {
        Button { mode = value } label: {
            Text(title)
                .font(.footnote.weight(.semibold))
                .foregroundStyle(mode == value ? Color.primary : Fx.secondaryText)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background { if mode == value { Capsule().fill(Fx.segment) } }
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
    }
}
