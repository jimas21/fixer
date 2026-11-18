import Foundation

/// Finished work the computer is holding for this iPhone with no screen waiting for it any more (Fixer was closed
/// or ended while it ran). Collected when the app opens and when the Queue screen refreshes, and filed where it was
/// started: a chat's result into that chat, Make's into Make. 11-17.
@MainActor
final class JobInbox {
    static let shared = JobInbox()
    /// Set by Chat and CreateStore at start-up.
    var toChat: ((_ conversation: UUID, _ kind: String, _ words: String, _ frames: [[String: Any]]) -> Void)?
    var toMake: ((_ kind: String, _ words: String, _ frames: [[String: Any]]) -> Void)?
    private var sweeping = false

    private init() {}

    func sweep() async {
        guard !sweeping, Desktop.shared.isOn, let link = Desktop.shared.link else { return }
        sweeping = true
        defer { sweeping = false }
        guard let q = try? await Desktop.shared.queueList() else { return }
        for job in q.jobs where job.mine && job.state == "ready" {
            if job.kind == "cutout" { continue }                          // its mask frame is not filed here: let it expire
            if await Desktop.live.contains(job.id) { continue }       // a screen on this phone is still following it
            // Only take it when the screen that asked for it can file it; otherwise it waits for the next sweep.
            let slot0 = job.slot ?? ""
            if slot0.hasPrefix("chat:"), toChat == nil { continue }
            if slot0.hasPrefix("make:"), toMake == nil { continue }
            var frames: [[String: Any]] = []
            var words = "", slot = slot0
            do {
                for try await f in Desktop.shared.watch(job.id, link: link) {
                    let t = f["t"] as? String ?? ""
                    if t == "queued" {
                        words = f["words"] as? String ?? ""
                        slot = f["slot"] as? String ?? slot
                    } else if t == "image" || t == "video" {
                        frames.append(f)
                    }
                }
            } catch { continue }
            await Desktop.live.markCollected(job.id)
            Desktop.shared.ack(job.id, link: link)
            if slot.hasPrefix("chat:"), let conv = UUID(uuidString: String(slot.dropFirst(5).prefix(36))) {
                toChat?(conv, job.kind, words, frames)
            } else if slot.hasPrefix("make:") {
                toMake?(job.kind, words, frames)
            }
        }
    }
}
