import CoreGraphics
import Foundation
import UIKit

/// Where a run reports: the chat's own working line (on the phone) or one of its background jobs (computer work).
/// 11-17: a chat's computer work runs as a job, so the conversation stays usable while it runs.
struct JobContext {
    let id: UUID
    let conversation: UUID
    let slot: String
}

/// Carries out a Plan on the current photo, one action at a time, posting results into the chat.
@MainActor
enum Agent {
    /// Mask for the brain's targets: for each box, the person/object Vision finds there, or the box itself.
    /// Runs off the main thread (Vision and full-size mask work used to freeze the screen).
    static func mask(for targets: [Plan.Target], in photo: CGImage) async -> CGImage? {
        let boxes = targets.compactMap(\.box)
        guard !boxes.isEmpty else { return nil }
        return await Task.detached(priority: .userInitiated) { Agent.maskNow(boxes, in: photo) }.value
    }

    nonisolated private static func maskNow(_ boxes: [CGRect], in photo: CGImage) -> CGImage? {
        let size = CGSize(width: photo.width, height: photo.height)
        let found = Subjects.pieces(in: photo)                  // once per photo, not once per target
        var mask: CGImage?
        for rel in boxes {
            let box = CGRect(x: rel.minX * size.width, y: rel.minY * size.height,
                             width: rel.width * size.width, height: rel.height * size.height)
            let piece = Subjects.piece(from: found, photo: photo, matching: box) ?? ImageKit.rectMask(box, size: size)
            mask = ImageKit.union(mask, piece)
        }
        return mask
    }

    /// Everything a plan will touch, for "ask before changing a photo".
    static func preview(_ plan: Plan, in photo: CGImage) async -> CGImage? {
        var m: CGImage?
        for a in plan.actions {
            switch a {
            case .remove(let t), .cutout(let t), .redraw(_, let t):
                let piece = await mask(for: t, in: photo)
                m = ImageKit.union(m, piece)
            default: break
            }
        }
        return m
    }

    private static func progress(_ chat: Chat) -> @Sendable (Double) -> Void {
        { p in Task { @MainActor in chat.progress = p } }
    }

    /// Where status, progress and the computer's job id go: the chat's thinking line, or the job's own bubble.
    private struct Report: @unchecked Sendable {
        let status: @MainActor (String) -> Void
        let progress: @Sendable (Double) -> Void
        let queued: @Sendable (String) -> Void
        let preview: @Sendable (Data) -> Void
        let slot: String?
        let conversation: UUID?
    }

    private static func report(_ chat: Chat, _ job: JobContext?) -> Report {
        guard let job else {
            return Report(status: { chat.busy = $0 }, progress: progress(chat), queued: { _ in },
                          preview: { _ in }, slot: nil, conversation: nil)
        }
        return Report(status: { chat.updateJob(job.id, status: $0) },
                      progress: { p in Task { @MainActor in chat.updateJob(job.id, progress: p) } },
                      queued: { id in Task { @MainActor in chat.updateJob(job.id, pcID: id) } },
                      preview: { data in
                          Task { @MainActor in
                              if let ui = UIImage(data: data) { chat.updateJob(job.id, preview: ui) }
                          }
                      },
                      slot: job.slot, conversation: job.conversation)
    }

    /// Work done on the iPhone itself. Inside a background job it waits for the chat's thinking to finish and keeps
    /// the chat from starting the phone brain meanwhile (the phone can't do both at once).
    private static func phone<T>(_ chat: Chat, _ job: JobContext?, _ work: () async throws -> T) async throws -> T {
        if job == nil { return try await work() }
        return try await chat.onPhone(work)
    }

    private static func needsDownload(_ m: CoreModel) -> Bool { !ModelStore.shared.isInstalled(m) }

    private static func noPhoto() -> ToolError { .model("Add a photo first.") }

    /// What the Library records about a result. When the computer drew it, the recipe and seed it reported come along
    /// (otherwise they are nil: the iPhone's own models don't report either).
    private static func made(_ action: String, words: String?, pc: Bool, rules: Rules) -> Made {
        let desktop = Desktop.shared
        var m = Made(action: action, words: words)
        m.choice = rules.chatModelChoice
        if pc, action == "make" || action == "edit" || action == "remove" {
            m.recipeID = desktop.lastImageRecipe
            m.seed = desktop.lastImageSeed
            m.enhanced = desktop.lastImageEnhanced
            m.model = desktop.lastImageRecipe ?? rules.pcImageID
        } else if action == "make" || action == "edit" {
            m.model = rules.generatorID
        }
        return m
    }

    /// A switch kept in Settings storage by the screens' own toggles ("Enhance my prompt"); `d` until it is first set.
    private static func flag(_ key: String, _ d: Bool) -> Bool {
        UserDefaults.standard.object(forKey: key) as? Bool ?? d
    }

    static func run(_ plan: Plan, chat: Chat, job: JobContext? = nil) async throws {
        let rules = Rules.shared
        let pc = Desktop.shared.isOn
        let r = report(chat, job)
        // 11-17: the photo and the painted area as they were when this started; the owner may add another photo
        // or paint again while the computer works.
        var photoNow = chat.cgImage
        let paintedNow = chat.mask
        // `creative`: a make or redraw the owner asked for (the chat model's explicit call applies, and the prompt may be
        // enhanced); removing an object is neither. `enhance` only reaches the computer: the phone never rewrites.
        let settings = { (prompt: String, remote: Bool, creative: Bool, enhance: Bool) -> Generator.Settings in
            var s = Generator.Settings(prompt: prompt, steps: rules.steps, guidance: rules.guidance, strength: rules.editStrength)
            s.status = { msg in Task { @MainActor in r.status(msg) } }
            s.remote = remote
            s.slot = r.slot
            s.queued = r.queued
            s.preview = r.preview
            if creative {
                s.explicit = plan.explicit
                s.enhance = enhance && remote
            }
            return s
        }
        for action in plan.actions {
            try Task.checkCancellation()
            switch action {
            case .remove(let targets):
                guard let photo = photoNow else { throw noPhoto() }
                guard let m = await mask(for: targets, in: photo) ?? paintedNow else { throw ToolError.noMask }
                var out: CGImage?
                if pc {
                    // On the computer, the photo editor repaints the area from an instruction, which handles big and
                    // textured areas far better than the phone's remover. The mask is grown so shadows and
                    // edges go too; only the masked area of the result is pasted back.
                    r.status("Removing…")
                    let what = names(targets) ?? "the object in the marked area"
                    let prompt = "Remove \(what) completely. Fill the space with the surrounding background so it looks natural, as if \(what) was never there. Keep everything else exactly the same."
                    let grown = ImageKit.softened(m, grow: max(6, CGFloat(photo.width) / 120), feather: 0)
                    do {
                        out = try await Generator.shared.redraw(photo, mask: grown, settings(prompt, true, false, false),
                                                                filter: rules.editFilter, progress: r.progress)
                    } catch let e as PCError where e.isUnreachable {
                        r.status("Your computer isn't reachable. Using this iPhone…")
                    }
                }
                if out == nil {
                    out = try await phone(chat, job) { () async throws -> CGImage in
                        r.status(needsDownload(.remover) ? "Downloading the remove tool…" : "Removing…")
                        return try await Remover.remove(photo, mask: m)
                    }
                }
                let done = out!
                chat.commit(done, caption: "Removed \(names(targets) ?? "the selected area").",
                            made: made("remove", words: names(targets), pc: pc, rules: rules), into: job?.conversation)
                photoNow = done

            case .cutout(let targets):
                guard let photo = photoNow else { throw noPhoto() }
                r.status("Cutting out…")
                let boxes = targets.compactMap(\.box)
                var picked: CGImage?
                // A painted area is used as painted; otherwise the computer's cut-out model does the edges.
                if pc, paintedNow == nil || !boxes.isEmpty {
                    do {
                        picked = try await Desktop.shared.cutoutMask(photo, boxes: boxes, labels: targets.map(\.label),
                                                                     slot: r.slot, queued: r.queued,
                                                                     status: { msg in Task { @MainActor in r.status(msg) } })
                    } catch let e as PCError where e.isUnreachable {
                        r.status("Your computer isn't reachable. Using this iPhone…")
                    }
                }
                if picked == nil { picked = await mask(for: targets, in: photo) ?? paintedNow }
                let out = try await Task.detached(priority: .userInitiated) { [picked] () throws -> CGImage in
                    let m = try picked ?? Subjects.allSubjects(in: photo)
                    guard let out = Subjects.cutOut(photo, mask: m) else { throw ToolError.model("Cut-out failed.") }
                    return out
                }.value
                chat.commit(out, caption: "Cut out \(names(targets) ?? "the subject").",
                            made: made("cutout", words: names(targets), pc: false, rules: rules), into: job?.conversation)
                photoNow = out

            case .upscale:
                guard let photo = photoNow else { throw noPhoto() }
                var out: CGImage?
                if pc {
                    r.status("Upscaling…")
                    do {
                        out = try await Desktop.shared.upscale(photo, slot: r.slot, queued: r.queued,
                                                               status: { msg in Task { @MainActor in r.status(msg) } },
                                                               progress: r.progress)
                    } catch let e as PCError where e.isUnreachable {
                        r.status("Your computer isn't reachable. Using this iPhone…")
                    }
                }
                if out == nil {
                    out = try await phone(chat, job) { () async throws -> CGImage in
                        r.status(needsDownload(.upscaler) ? "Downloading the upscaler…" : "Upscaling…")
                        return try await Upscaler.upscale(photo, progress: r.progress)
                    }
                }
                let done = out!
                chat.commit(done, caption: "Upscaled to \(done.width)×\(done.height).",
                            made: made("upscale", words: nil, pc: false, rules: rules), into: job?.conversation)
                photoNow = done

            case .redraw(let prompt, let targets):
                guard let photo = photoNow else { throw noPhoto() }
                let m = await mask(for: targets, in: photo) ?? paintedNow
                let text = prompt.isEmpty ? "high quality photo" : prompt
                let out = try await generate(chat, job: job, r: r, pc: pc, label: "Editing…") { remote in
                    try await Generator.shared.redraw(photo, mask: m,
                                                      settings(text, remote, true, flag("make.enhanceEdits", false)),
                                                      filter: rules.editFilter, progress: r.progress)
                }
                chat.commit(out, caption: "Edited \(names(targets) ?? (m == nil ? "the photo" : "the selected area")).",
                            made: made("edit", words: text, pc: pc, rules: rules), into: job?.conversation)
                photoNow = out

            case .search:
                break                                   // handled by the chat before the plan runs

            case .queueCancel, .queueMove, .stopAll, .freeMemory:
                break                                   // handled by `control` before any picture work, never reached here

            case .make(let prompt):
                let out = try await generate(chat, job: job, r: r, pc: pc, label: "Creating image…") { remote in
                    try await Generator.shared.make(settings(prompt, remote, true, flag("chat.enhance", true)),
                                                    filter: rules.newFilter, progress: r.progress)
                }
                // No caption: the words it was made from are kept in the picture's details (Library / viewer).
                chat.commit(out, caption: "", made: made("make", words: prompt, pc: pc, rules: rules), into: job?.conversation)
                photoNow = out

            case .video(let prompt, let fromPhoto):
                guard pc else { throw ToolError.model("Creating videos needs Fixer Desktop on your computer.") }
                let start = fromPhoto ? photoNow : nil
                if fromPhoto, start == nil { throw noPhoto() }
                r.status("Creating video…")
                // The chat model's own call when it made one; otherwise the computer decides from the words.
                let explicitWord = plan.explicit.map { $0 ? "on" : "off" } ?? "auto"
                let data = try await Desktop.shared.video(prompt: prompt.isEmpty ? "natural, gentle motion" : prompt,
                                                          start: start, explicit: explicitWord, slot: r.slot, queued: r.queued,
                                                          status: { msg in Task { @MainActor in r.status(msg) } },
                                                          progress: r.progress)
                let f = fromPhoto ? rules.editFilter : rules.newFilter
                if await ContentGate.blocksVideo(data, f) { throw ToolError.blocked }
                var info = Made(action: fromPhoto ? "animate" : "video", words: prompt.isEmpty ? nil : prompt)
                info.model = rules.pcVideoID
                info.explicitVideo = explicitWord
                chat.commitVideo(data, made: info, into: job?.conversation)
            }
            if job == nil { chat.progress = nil }
        }
    }

    /// The brain's line actions (11-17): run at once, before any picture work in the same reply. Says something only
    /// when it didn't work, or when the brain said nothing.
    static func control(_ actions: [Plan.Action], chat: Chat, said: Bool) async {
        let desktop = Desktop.shared
        for a in actions {
            do {
                switch a {
                case .queueCancel(let ids):
                    let n = try await desktop.cancelJobs(ids)
                    if n == 0 { chat.say("Nothing to stop there: it had already finished or gone.") }
                case .queueMove(let id, let to):
                    let k = try await desktop.moveJob(id, to: to)
                    if !said { chat.say("Moved it to place \(k) in line.") }
                case .stopAll:
                    let n = try await desktop.stopAllNow()
                    if !said { chat.say(n == 0 ? "Nothing was running." : "Stopped everything on your computer.") }
                case .freeMemory:
                    if try await desktop.freeMemory() == nil { chat.say("Your computer needs the newer Fixer Desktop for that.") }
                    else if !said { chat.say("Freed your computer's memory.") }
                default: break
                }
            } catch {
                chat.say(Desktop.isOldComputer(error) ? "Your computer needs the newer Fixer Desktop for that."
                                                      : error.localizedDescription)
            }
        }
    }

    /// A short title for the job bubble and the line.
    static func title(_ plan: Plan) -> String {
        for a in plan.actions {
            switch a {
            case .make: return "Making a picture"
            case .redraw: return "Changing the photo"
            case .remove: return "Removing"
            case .cutout: return "Cutting out"
            case .upscale: return "Sharpening and enlarging"
            case .video(_, let fromPhoto): return fromPhoto ? "Animating the photo" : "Making a video"
            default: continue
            }
        }
        return "Working"
    }

    /// Image work runs on the computer when Fixer Desktop is on; if it can't be reached, on this iPhone instead.
    private static func generate(_ chat: Chat, job: JobContext?, r: Report, pc: Bool, label: String,
                                 _ work: (_ remote: Bool) async throws -> CGImage) async throws -> CGImage {
        if pc {
            r.status(label)
            do { return try await work(true) }
            catch let e as PCError where e.isUnreachable {
                r.status("Your computer isn't reachable. Using this iPhone…")
            }
        }
        return try await phone(chat, job) { () async throws -> CGImage in
            Brain.shared.unload()                               // make room for the image model
            r.status(needsDownload(.generator) ? "Downloading image model (1 GB)…" : label)
            try await ModelStore.shared.ensure(.generator)
            r.status(label)
            return try await work(false)
        }
    }

    private static func names(_ targets: [Plan.Target]) -> String? {
        let labels = targets.map(\.label).filter { !$0.isEmpty }
        guard !labels.isEmpty else { return nil }
        return labels.count == 1 ? labels[0] : labels.dropLast().joined(separator: ", ") + " and " + labels.last!
    }
}
