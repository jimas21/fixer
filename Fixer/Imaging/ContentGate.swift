import AVFoundation
import CoreGraphics
import Foundation

/// The one content-filter check every result of Fixer's own making goes through (chat pictures from the computer,
/// Make, revisions and videos). Off means no check at all, so a run isn't slowed. Remove, cut out and upscale make
/// nothing new and are never checked.
enum ContentGate {
    /// True when the picture should be refused. The classifier runs off the main thread.
    static func blocks(_ image: CGImage, _ filter: FilterSetting) async -> Bool {
        if filter.isOff { return false }
        return await Task.detached(priority: .userInitiated) { Detector.blocks(image, filter) }.value
    }

    /// True when any of three frames (10%, 50%, 90% in) of the video trips the filter.
    static func blocksVideo(_ data: Data, _ filter: FilterSetting) async -> Bool {
        if filter.isOff { return false }
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("check-\(UUID().uuidString).mp4")
        // UnlessOpen, not complete: the copy is read straight back and a lock mid-check must not make the read fail.
        do { try data.write(to: url, options: .completeFileProtectionUnlessOpen) } catch { return false }
        defer { try? FileManager.default.removeItem(at: url) }
        let asset = AVURLAsset(url: url)
        guard let duration = try? await asset.load(.duration), duration.seconds > 0 else { return false }
        let generator = AVAssetImageGenerator(asset: asset)
        generator.appliesPreferredTrackTransform = true
        generator.maximumSize = CGSize(width: 1024, height: 1024)
        for fraction in [0.1, 0.5, 0.9] {
            let time = CMTime(seconds: duration.seconds * fraction, preferredTimescale: 600)
            if let frame = try? await generator.image(at: time).image, await blocks(frame, filter) { return true }
        }
        return false
    }
}
