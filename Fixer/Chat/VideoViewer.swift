import AVFoundation
import SwiftUI
import UIKit

/// A video in the conversation: plays inline at its own shape; the corner button (or a double-tap) opens it full
/// screen. Long-press to share.
struct InlineVideo: View {
    let url: URL
    @State private var player: AVPlayer?
    @State private var aspect: CGFloat = 16 / 9            // width / height, read from the clip
    var onFull: () -> Void = {}                             // opens the shared viewer on this video

    var body: some View {
        let width: CGFloat = aspect >= 1 ? 260 : 200
        VideoPlayerLayerView(player: player)
            .frame(width: width, height: width / max(0.2, aspect))
            .background(Color.black)
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .overlay(alignment: .bottomLeading) {
                Image(systemName: "play.fill").font(.caption).foregroundStyle(.white.opacity(0.8)).padding(8)
            }
            .overlay(alignment: .topTrailing) {
                Button { player?.pause(); onFull() } label: {
                    Image(systemName: "arrow.up.left.and.arrow.down.right")
                        .font(.footnote.weight(.semibold)).foregroundStyle(.white)
                        .padding(7).background(.black.opacity(0.45), in: Circle())
                }
                .padding(6)
            }
            .onTapGesture(count: 2) { player?.pause(); onFull() }
            .onTapGesture {
                guard let player else { return }
                if player.timeControlStatus == .playing { player.pause(); return }
                if let d = player.currentItem?.duration, d.isNumeric,
                   player.currentTime() >= d - CMTime(value: 1, timescale: 30) {
                    player.seek(to: .zero)                     // finished: play again from the start
                }
                player.play()
            }
            .task {
                if player == nil { player = AVPlayer(url: url) }
                if let a = await VideoKit.aspect(url) { aspect = a }
            }
            .onDisappear { player?.pause() }
            .contextMenu { ShareLink(item: url) }
    }
}

enum VideoKit {
    /// Display width / height of a clip (rotation applied), or nil if it can't be read.
    static func aspect(_ url: URL) async -> CGFloat? {
        let asset = AVURLAsset(url: url)
        guard let track = try? await asset.loadTracks(withMediaType: .video).first,
              let loaded = try? await track.load(.naturalSize, .preferredTransform) else { return nil }
        let r = CGRect(origin: .zero, size: loaded.0).applying(loaded.1)
        guard r.height != 0 else { return nil }
        return abs(r.width) / abs(r.height)
    }
}

/// A UIView whose layer is an AVPlayerLayer (aspect-fit).
final class PlayerLayerView: UIView {
    override static var layerClass: AnyClass { AVPlayerLayer.self }
    var playerLayer: AVPlayerLayer { layer as! AVPlayerLayer }   // swiftlint:disable:this force_cast
}

/// The inline player: just the picture, no system controls (tap toggles play, the corner button opens full screen).
struct VideoPlayerLayerView: UIViewRepresentable {
    let player: AVPlayer?

    func makeUIView(context: Context) -> PlayerLayerView {
        let v = PlayerLayerView()
        v.playerLayer.videoGravity = .resizeAspect
        v.backgroundColor = .black
        return v
    }

    func updateUIView(_ v: PlayerLayerView, context: Context) {
        if v.playerLayer.player !== player { v.playerLayer.player = player }
    }
}

/// The full-screen player inside a scroll view: pinch to zoom up to 8x, drag while zoomed, double-tap to zoom in or
/// back out (same feel as the pictures).
struct ZoomableVideo: UIViewRepresentable {
    let player: AVPlayer

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
        let view = PlayerLayerView()
        view.playerLayer.videoGravity = .resizeAspect
        view.playerLayer.player = player
        view.backgroundColor = .black
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
        context.coordinator.content = view
        let doubleTap = UITapGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.doubleTapped(_:)))
        doubleTap.numberOfTapsRequired = 2
        scroll.addGestureRecognizer(doubleTap)
        return scroll
    }

    func updateUIView(_ scroll: UIScrollView, context: Context) {}

    func makeCoordinator() -> Coordinator { Coordinator() }

    final class Coordinator: NSObject, UIScrollViewDelegate {
        weak var content: UIView?

        func viewForZooming(in scrollView: UIScrollView) -> UIView? { content }

        @objc func doubleTapped(_ gesture: UITapGestureRecognizer) {
            guard let scroll = gesture.view as? UIScrollView, let content else { return }
            if scroll.zoomScale > 1.01 {
                scroll.setZoomScale(1, animated: true)
            } else {
                let point = gesture.location(in: content)
                let size = CGSize(width: scroll.bounds.width / 3, height: scroll.bounds.height / 3)
                scroll.zoom(to: CGRect(x: point.x - size.width / 2, y: point.y - size.height / 2,
                                       width: size.width, height: size.height), animated: true)
            }
        }
    }
}
