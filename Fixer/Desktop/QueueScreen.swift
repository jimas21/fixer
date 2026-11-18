import SwiftUI

/// Everything the computer is doing or holding for the phone (11-17, "it keeps getting backed up and I can't do
/// anything"): what's being made now with its progress, what waits in line (stop, move up, run next), finished work
/// waiting to be collected, how full the graphics card and memory are, Stop everything and Free memory.
struct QueueScreen: View {
    var inSheet = false
    @ObservedObject private var desktop = Desktop.shared
    @Environment(\.dismiss) private var dismiss
    @State private var queue: PCQueue?
    @State private var failed: String?
    @State private var oldComputer = false
    @State private var confirmStop = false
    @State private var confirmFree = false
    @State private var note: String?
    @State private var working = false

    private var running: [PCJob] { (queue?.jobs ?? []).filter { $0.state == "running" } }
    private var waiting: [PCJob] {
        (queue?.jobs ?? []).filter { $0.state == "waiting" }.sorted { ($0.position ?? 0) < ($1.position ?? 0) }
    }
    private var held: [PCJob] { (queue?.jobs ?? []).filter { $0.state == "ready" || $0.state == "failed" } }

    var body: some View {
        Form {
            if !desktop.isOn {
                Text("Your computer isn't connected.")
            } else if oldComputer {
                Section {
                    Text("Update Fixer Desktop on your computer to see its work here.")
                    Button("Stop everything", role: .destructive) { desktop.stopAll() }
                }
            } else {
                if let note {
                    Section { Text(note).font(.footnote).foregroundStyle(.secondary) }
                } else if let failed, queue == nil {
                    Section { Text(failed).font(.footnote).foregroundStyle(.secondary) }
                }
                nowSection
                lineSection
                if !held.isEmpty { heldSection }
                computerSection
                recentSection
                actionsSection
            }
        }
        .navigationTitle("Your computer's work")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            if inSheet {
                ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() } }
            }
        }
        .task {
            while !Task.isCancelled {
                await refresh()
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
        .refreshable { await refresh() }
        .confirmationDialog("Stop everything?", isPresented: $confirmStop, titleVisibility: .visible) {
            Button("Stop everything", role: .destructive) { act { _ = try await desktop.stopAllNow() } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Everything being made or waiting on your computer stops. Finished work it was holding is dropped.")
        }
        .confirmationDialog("Free memory?", isPresented: $confirmFree, titleVisibility: .visible) {
            Button("Free memory", role: .destructive) { act { _ = try await desktop.freeMemory() } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This stops everything and unloads every model from your computer's graphics card and memory.")
        }
    }

    // MARK: sections

    @ViewBuilder private var nowSection: some View {
        Section("Now") {
            if running.isEmpty {
                Text("Nothing is being made right now.").foregroundStyle(.secondary)
            }
            ForEach(running) { job in
                VStack(alignment: .leading, spacing: 6) {
                    Text(Self.kindWord(job.kind) + ": " + job.label).lineLimit(2)
                    if let p = job.progress { ProgressView(value: min(max(p, 0), 1)) }
                    HStack {
                        Text(job.status).font(.footnote).foregroundStyle(.secondary)
                        if let eta = job.etaSeconds, eta > 0 {
                            Text("· about \(max(1, Int((Double(eta) / 60).rounded()))) min left")
                                .font(.footnote).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button("Stop", role: .destructive) { act { _ = try await desktop.cancelJobs([job.id]) } }
                            .buttonStyle(.bordered)
                            .disabled(working)
                    }
                }
            }
        }
    }

    @ViewBuilder private var lineSection: some View {
        if !waiting.isEmpty {
            Section {
                ForEach(waiting) { job in
                    HStack(spacing: 10) {
                        Text(Self.ordinal(job.position ?? 0)).font(.footnote.bold()).foregroundStyle(.secondary)
                            .frame(minWidth: 34, alignment: .leading)
                        Text(Self.kindWord(job.kind) + ": " + job.label).lineLimit(2)
                    }
                    .swipeActions(edge: .trailing) {
                        Button(role: .destructive) { act { _ = try await desktop.cancelJobs([job.id]) } } label: {
                            Label("Stop", systemImage: "xmark")
                        }
                    }
                    .swipeActions(edge: .leading) {
                        Button { act { _ = try await desktop.moveJob(job.id, to: 1) } } label: {
                            Label("Next", systemImage: "arrow.up.to.line")
                        }
                        .tint(.blue)
                    }
                    .contextMenu {
                        Button("Make it next") { act { _ = try await desktop.moveJob(job.id, to: 1) } }
                        Button("Move up") { act { _ = try await desktop.moveJob(job.id, to: max(1, (job.position ?? 1) - 1)) } }
                        Button("Move down") { act { _ = try await desktop.moveJob(job.id, to: (job.position ?? 1) + 1) } }
                        Button("Stop", role: .destructive) { act { _ = try await desktop.cancelJobs([job.id]) } }
                    }
                }
            } header: {
                Text("In line")
            } footer: {
                Text("Pictures, edits and videos are made one at a time. A new request from the same chat or Make card replaces its own unfinished one.")
            }
        }
    }

    @ViewBuilder private var heldSection: some View {
        Section("Finished, not collected") {
            ForEach(held) { job in
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text(Self.kindWord(job.kind) + ": " + job.label).lineLimit(2)
                        Spacer()
                        if job.state == "ready", job.mine {
                            Button("Get it") { Task { await JobInbox.shared.sweep() } }
                                .buttonStyle(.bordered)
                        }
                    }
                    if job.state == "failed" {
                        Text(job.error ?? "It didn't work.").font(.footnote).foregroundStyle(.secondary)
                    } else if let keep = job.keepSeconds {
                        Text("Kept for \(max(1, Int((Double(keep) / 60).rounded()))) more min")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                }
                .swipeActions(edge: .trailing) {
                    Button(role: .destructive) { act { _ = try await desktop.cancelJobs([job.id]) } } label: {
                        Label("Remove", systemImage: "trash")
                    }
                }
            }
        }
    }

    @ViewBuilder private var computerSection: some View {
        if let q = queue {
            Section("Your computer") {
                VStack(alignment: .leading, spacing: 6) {
                    LabeledContent("Graphics card memory", value: "\(Self.fmt(q.cardUsed)) of \(Self.fmt(q.cardTotal)) GB in use")
                    ProgressView(value: min(q.cardUsed, max(q.cardTotal, 0.1)), total: max(q.cardTotal, 0.1))
                }
                VStack(alignment: .leading, spacing: 6) {
                    LabeledContent("Memory", value: "\(Self.fmt(q.memUsed)) of \(Self.fmt(q.memTotal)) GB in use")
                    ProgressView(value: min(q.memUsed, max(q.memTotal, 0.1)), total: max(q.memTotal, 0.1))
                }
                LabeledContent("Chat", value: brainText(q))
            }
        }
    }

    /// How long things really took on this computer since it started (11-17), so models can be compared on speed.
    @ViewBuilder private var recentSection: some View {
        if let q = queue, !q.averages.isEmpty || !q.recent.isEmpty {
            Section {
                ForEach(q.averages) { a in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(a.model + " · " + a.kind + (a.explicit ? " · explicit add-on" : "")).lineLimit(2)
                        Text(averageLine(a)).font(.footnote).foregroundStyle(.secondary)
                    }
                }
                if !q.recent.isEmpty {
                    DisclosureGroup("Last \(q.recent.count) jobs") {
                        ForEach(q.recent) { t in
                            VStack(alignment: .leading, spacing: 2) {
                                Text(timingTitle(t)).font(.footnote).lineLimit(2)
                                Text(timingLine(t)).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            } header: {
                Text("Recent (this computer, since it started)")
            } footer: {
                Text("Measured from the work it really did, never from test runs. “Had to load a model” means that job waited for a model to be read from disk.")
            }
        }
    }

    private func averageLine(_ a: PCTimingAverage) -> String {
        var line = "\(Self.secs(a.totalS)) usually (\(a.n) made)"
        if let warm = a.warmTotalS, a.coldTotalS != nil { line += " · \(Self.secs(warm)) when ready" }
        if let cold = a.coldTotalS { line += " · \(Self.secs(cold)) when it had to load" }
        return line
    }

    private func timingTitle(_ t: PCTiming) -> String {
        var parts = [t.model.isEmpty ? Self.kindWord(t.kind) : t.model, Self.kindWord(t.kind).lowercased()]
        if let mp = t.mp { parts.append("\(Self.fmt(mp)) MP") }
        if let steps = t.steps { parts.append("\(steps) steps") }
        if t.explicit { parts.append("explicit add-on") }
        return parts.joined(separator: " · ")
    }

    private func timingLine(_ t: PCTiming) -> String {
        var parts = [Self.secs(t.totalS)]
        if t.loadS >= 0.5 { parts.append("loading \(Self.secs(t.loadS))") }
        if t.drawS >= 0.5 { parts.append("drawing \(Self.secs(t.drawS))") }
        if t.enhance, t.enhanceS >= 0.5 { parts.append("enhancing \(Self.secs(t.enhanceS))") }
        if t.loaded { parts.append("had to load a model") }
        if !t.ok { parts.append("didn't finish") }
        return parts.joined(separator: " · ")
    }

    @ViewBuilder private var actionsSection: some View {
        Section {
            Button("Stop everything", role: .destructive) {
                if running.isEmpty && waiting.isEmpty && held.isEmpty { act { _ = try await desktop.stopAllNow() } }
                else { confirmStop = true }
            }
            .disabled(working)
            Button("Free memory") { confirmFree = true }
                .disabled(working)
        } footer: {
            Text("Free memory stops everything and unloads every model from your computer's graphics card and memory. The next message or picture takes a little longer to start.")
        }
    }

    // MARK: work

    private func brainText(_ q: PCQueue) -> String {
        switch q.brainState {
        case "ready": return "Ready"
        case "loading": return "Starting…"
        case "paused": return "Paused while a video is made"
        default: return "Off (starts with your next message)"
        }
    }

    private func refresh() async {
        guard desktop.isOn else { return }
        do {
            let q = try await desktop.queueList()
            queue = q
            failed = nil
            oldComputer = false
            // Finished work for this iPhone is collected where it was asked for; the row goes on the next refresh.
            if q.jobs.contains(where: { $0.mine && $0.state == "ready" }) { Task { await JobInbox.shared.sweep() } }
        } catch {
            if Desktop.isOldComputer(error) { oldComputer = true } else { failed = error.localizedDescription }
        }
    }

    /// Runs one action, shows its error if it failed, and refreshes.
    private func act(_ work: @escaping () async throws -> Void) {
        Task {
            working = true
            note = nil
            do { try await work() } catch { note = error.localizedDescription }
            working = false
            await refresh()
        }
    }

    // MARK: words

    static func kindWord(_ kind: String) -> String {
        switch kind {
        case "picture": return "Picture"
        case "pictures": return "Pictures"
        case "edit": return "Photo edit"
        case "change": return "Change"
        case "video": return "Video"
        case "cutout": return "Cut-out"
        case "upscale": return "Sharpen and enlarge"
        case "remove": return "Removing a model"
        default: return "Work"
        }
    }

    static func ordinal(_ n: Int) -> String {
        let tens = n % 100
        if (11...13).contains(tens) { return "\(n)th" }
        switch n % 10 {
        case 1: return "\(n)st"
        case 2: return "\(n)nd"
        case 3: return "\(n)rd"
        default: return "\(n)th"
        }
    }

    static func fmt(_ x: Double) -> String { String(format: "%.1f", x) }

    /// "9.4 s" for short times, "41 s" once it is past a hundred.
    static func secs(_ x: Double) -> String { x >= 100 ? "\(Int(x.rounded())) s" : "\(fmt(x)) s" }
}
