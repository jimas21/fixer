import SwiftUI
import UIKit

/// The two bits of the old canvas the compose sheet still uses (11-18): the empty state shown inside the picture card
/// at the top, and the roll of this session's pictures under it. The full-bleed canvas, its blurred backdrop, the
/// paging swipe and the floating panel were retired when Make became one compose sheet.
extension CreateScreen {
    // MARK: empty state (inside the picture card)

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
        .onTapGesture { if focus != nil || keyboard.visible { focus = nil; Self.hideKeyboard() } }
    }

    // MARK: the roll

    /// Everything made this session as a strip of thumbnails under the picture, oldest on the left. Tap one to put it
    /// on the canvas; tap the one that is showing to open it full screen. The strip keeps the one on the canvas in the
    /// middle, and ends with a way into the Library.
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
}
