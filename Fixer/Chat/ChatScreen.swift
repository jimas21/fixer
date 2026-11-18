import AVKit
import PhotosUI
import SwiftUI

/// The whole app: one full-height conversation, like any AI chat. Photos and results appear inside it. The photo
/// being worked on shows as a small thumbnail above the message box; tap any picture to open it full screen
/// (paint an area, remove, cut out, upscale, undo, save, share).
struct ChatScreen: View {
    @ObservedObject var chat: Chat
    @EnvironmentObject private var router: AppRouter
    @ObservedObject private var store = ModelStore.shared
    @ObservedObject private var desktop = Desktop.shared
    @ObservedObject private var rules = Rules.shared
    @State private var input = ""
    @State private var attachment: UIImage?
    @State private var pick: PhotosPickerItem?
    @State private var viewer: ChatViewerStart?
    @State private var showHistory = false
    @State private var showPicker = false
    @State private var showQueue = false    // 11-17: what the computer is making and what waits
    @State private var cleared = 0          // bumped by "clear everything"
    @State private var showCamera = false
    @EnvironmentObject private var keyboard: KeyboardObserver
    @FocusState private var typing: Bool

    var body: some View {
        NavigationStack {
            messageList
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    VStack(spacing: 6) {
                        BrainStatusBar()
                        QuietComposer(chat: chat, input: $input, attachment: $attachment, typing: $typing,
                                      onPhotos: { showPicker = true },
                                      onCamera: { showCamera = true },
                                      onQueue: { showQueue = true },
                                      onOpenPhoto: {
                                          if let last = chat.messages.last(where: { $0.image != nil }) {
                                              viewer = ChatViewerStart(id: last.id)
                                          }
                                      },
                                      onSend: send)
                    }
                }
                .tabBarClearance()
                .background(Fx.canvas.ignoresSafeArea())
            .navigationTitle(chat.conversation.title == "New chat" ? "Fixer" : chat.conversation.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbarBackground(.hidden, for: .navigationBar)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { showHistory = true } label: { Image(systemName: "clock.arrow.circlepath") }
                        .buttonStyle(GlassCircleButtonStyle())
                        .accessibilityLabel("History")
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { chat.newChat() } label: { Image(systemName: "square.and.pencil") }
                        .buttonStyle(GlassCircleButtonStyle())
                        .accessibilityLabel("New chat")
                }
            }
            .sheet(isPresented: $showHistory) {
                HistorySheet(chat: chat, onCleared: { router.clearedCount += 1 })
            }
            .sheet(isPresented: $showQueue) { NavigationStack { QueueScreen(inSheet: true) } }
            .photosPicker(isPresented: $showPicker, selection: $pick, matching: .images)
            .fullScreenCover(isPresented: $showCamera) {
                CameraPicker { image in
                    showCamera = false
                    if let image { attachment = image; focusSoon() }       // next: say what to change
                }
                .ignoresSafeArea()
            }
            .onChange(of: router.clearedCount) { _, _ in
                input = ""; attachment = nil; pick = nil; viewer = nil; cleared += 1
            }
            .sensoryFeedback(.success, trigger: chat.doneCount)
            .sensoryFeedback(.error, trigger: chat.errorCount)
            .sensoryFeedback(.impact, trigger: chat.sendCount)
            .overlay(alignment: .top) { toast }
            .fullScreenCover(item: $viewer) { start in ChatMediaViewer(chat: chat, router: router, start: start.id) }
            .onChange(of: pick) { _, item in
                let generation = cleared
                Task {
                    if let data = try? await item?.loadTransferable(type: Data.self), let ui = UIImage(data: data),
                       generation == cleared {                 // not if "clear everything" ran meanwhile
                        attachment = ui
                        focusSoon()                              // next: say what to change
                    }
                    pick = nil
                }
            }
            .confirmationDialog("Make a video?",
                                isPresented: Binding(get: { chat.pendingPlan != nil }, set: { if !$0 { chat.pendingPlan = nil } }),
                                titleVisibility: .visible) {
                Button("Make video") { if let p = chat.pendingPlan { chat.pendingPlan = nil; chat.run(p) } }
                Button("Cancel", role: .cancel) { chat.pendingPlan = nil }
            } message: {
                Text("About 2–3 minutes on your computer.")
            }
        }
    }

    /// A short message that hides itself (saved to Photos, can't save, and so on).
    @ViewBuilder private var toast: some View {
        if let text = chat.notice {
            Text(text)
                .font(.subheadline)
                .padding(.horizontal, 14).padding(.vertical, 10)
                .background(.regularMaterial, in: Capsule())
                .padding(.top, 8)
                .transition(.move(edge: .top).combined(with: .opacity))
                .task(id: text) {
                    try? await Task.sleep(nanoseconds: 3_000_000_000)
                    withAnimation { chat.notice = nil }
                }
                .onTapGesture { chat.notice = nil }
        }
    }

    // MARK: messages

    private var messageList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    if chat.messages.isEmpty {
                        emptyState
                    }
                    ForEach(chat.messages) { m in
                        // The reply being written shows its progress instead of an empty bubble.
                        if m.id == chat.messages.last?.id, m.role == .fixer, m.text.isEmpty, m.image == nil, m.video == nil,
                           chat.busy != nil {
                            EmptyView()
                        } else {
                            MessageView(message: m, conversation: chat.conversation.id,
                                        onOpenPhoto: { viewer = ChatViewerStart(id: m.id) },
                                        onOpenVideo: { viewer = ChatViewerStart(id: m.id) },
                                        onRetry: { chat.retry(m.id) },
                                        onOpenQueue: { showQueue = true })
                                .id(m.id)
                        }
                    }
                    if let busy = chat.busy {
                        // 11-17: Stop here ends the thinking only; the computer's pictures and videos are the job
                        // bubbles' own (each has its own Stop), so a chat reply no longer stops them.
                        WorkingRow(text: busy, progress: chat.progress ?? store.progress.values.first,
                                   showLine: desktop.isOn ? { showQueue = true } : nil)
                            .id("working")
                    }
                    ForEach(chat.jobs.filter { $0.conversation == chat.conversation.id }) { job in
                        JobCard(job: job, stop: { chat.cancelJob(job.id) }, showLine: { showQueue = true })
                            .id(job.id)
                    }
                    Color.clear.frame(height: 1).id("bottom")
                }
                .padding()
            }
            .scrollDismissesKeyboard(.interactively)
            .simultaneousGesture(TapGesture().onEnded { typing = false })
            .onChange(of: keyboard.visible) { _, up in if up { scrollDown(proxy) } }
            .onChange(of: chat.messages.last?.text) { _, _ in scrollDown(proxy) }
            .onChange(of: chat.messages.count) { _, _ in scrollDown(proxy) }
            .onChange(of: chat.busy) { _, _ in scrollDown(proxy) }
            .onChange(of: chat.jobs.count) { _, _ in scrollDown(proxy) }
            .onChange(of: chat.jobs.filter { $0.preview != nil }.map(\.id)) { _, _ in scrollDown(proxy) }
        }
    }

    private var emptyState: some View {
        VStack(spacing: 16) {
            Image(systemName: "wand.and.stars").font(.system(size: 36)).foregroundStyle(.secondary)
            Text("What would you like to do?").font(.headline)
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)], spacing: 12) {
                startTile("Make a picture", "photo.badge.plus", "Describe it and Fixer draws it") {
                    input = "Make a picture of "; focusSoon()
                }
                startTile("Edit a photo", "wand.and.stars", "Pick a photo, then say what to change") {
                    showPicker = true
                }
                startTile("Ask anything", "bubble.left", "Questions, ideas, answers") {
                    focusSoon()
                }
                startTile("Make with full control", "paintbrush.pointed", "Choose the model and every setting") {
                    router.tab = .make
                }
            }
        }
        .padding(.top, 40)
    }

    /// Focus the message field once the tap (or the photo picker closing) has finished; set in the same moment it
    /// was dropped and the keyboard never came up.
    private func focusSoon() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { typing = true }
    }

    private func startTile(_ title: String, _ icon: String, _ note: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 6) {
                Image(systemName: icon).font(.title3)
                Text(title).font(.subheadline.weight(.semibold)).multilineTextAlignment(.leading)
                Text(note).font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.leading)
            }
            .frame(maxWidth: .infinity, minHeight: 96, alignment: .topLeading)
            .padding(12)
            .background(Color(.secondarySystemBackground))
            .clipShape(RoundedRectangle(cornerRadius: 14))
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
    }

    private func scrollDown(_ proxy: ScrollViewProxy) {
        // Everything that can be last (the reply being written, a job card) sits above this marker.
        withAnimation { proxy.scrollTo("bottom", anchor: .bottom) }
    }

    // MARK: input

    private func send() {
        let text = input
        let image = attachment
        input = ""
        attachment = nil
        chat.send(text, attachment: image)
    }
}

/// Which message the viewer opens on.
struct ChatViewerStart: Identifiable { let id: UUID }

/// The chat's side of the shared viewer: every picture and video in the conversation, newest last. Any picture can be
/// changed, painted, cut out, upscaled or animated; the work runs in the conversation, so the viewer closes.
struct ChatMediaViewer: View {
    @ObservedObject var chat: Chat
    let router: AppRouter
    @State private var current: UUID

    init(chat: Chat, router: AppRouter, start: UUID) {
        self.chat = chat
        self.router = router
        _current = State(initialValue: start)
    }

    private var items: [MediaItem] {
        let conv = chat.conversation.id
        return chat.messages.compactMap { m -> MediaItem? in
            if let file = m.image {
                return MediaItem(id: m.id, kind: .picture, source: .chat(conv: conv, file: file), libraryID: m.libraryID,
                                 enhanced: m.enhanced)
            }
            if let video = m.video {
                return MediaItem(id: m.id, kind: .video, source: .chatVideo(History.videoURL(video, in: conv)),
                                 libraryID: m.libraryID)
            }
            return nil
        }
    }

    private var actions: MediaActions {
        var a = MediaActions()
        a.closesAfterAction = true
        a.undo = { [chat] in chat.undo() }
        a.canUndo = !chat.undoStack.isEmpty
        a.change = { [chat] item, words, mask, _ in chat.apply(.redraw(words, []), to: item.id, mask: mask) }
        a.remove = { [chat] item, mask in chat.apply(.remove([]), to: item.id, mask: mask) }
        a.cutOut = { [chat] item, mask in chat.apply(.cutout([]), to: item.id, mask: mask) }
        a.upscale = { [chat] item in chat.apply(.upscale, to: item.id) }
        a.animate = { [chat] item, words in chat.apply(.video(words, fromPhoto: true), to: item.id) }
        a.makeAnother = { [chat, router] item in
            // The Library copy knows the model, words and seed. It can only be read while the Library is open.
            if let lid = item.libraryID, let entry = LibraryStore.shared.entries.first(where: { $0.id == lid }) {
                router.makeAgain = entry.meta
                router.tab = .make
            } else {
                chat.notice = "Unlock the Library once, then try again."
            }
        }
        return a
    }

    var body: some View {
        MediaViewer(items: items, current: $current, actions: actions, busy: chat.busy)
    }
}

/// "Enhanced prompt": the words the computer's chat model wrote from the owner's, shown small under a picture.
struct EnhancedPromptNote: View {
    let text: String
    var body: some View {
        DisclosureGroup {
            Text(text)
                .font(.footnote)
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        } label: {
            Label("Enhanced prompt", systemImage: "sparkles")
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 10)
        .frame(maxWidth: 260)
    }
}

struct MessageView: View {
    let message: Message
    let conversation: UUID
    let onOpenPhoto: () -> Void
    var onOpenVideo: () -> Void = {}
    var onRetry: () -> Void = {}
    var onOpenQueue: () -> Void = {}

    var body: some View {
        HStack {
            if message.role == .me { Spacer(minLength: 40) }
            VStack(alignment: message.role == .me ? .trailing : .leading, spacing: 6) {
                if let name = message.image, let ui = History.image(name, in: conversation) {
                    Button(action: onOpenPhoto) {
                        Image(uiImage: ui)
                            .resizable()
                            .scaledToFit()
                            .frame(maxWidth: 260, maxHeight: 300)
                            .clipShape(RoundedRectangle(cornerRadius: Fx.bubbleRadius, style: .continuous))
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Open picture")
                    .contextMenu {
                        ShareLink(item: Image(uiImage: ui), preview: SharePreview("Photo", image: Image(uiImage: ui)))
                    }
                    if let enhanced = message.enhanced, !enhanced.isEmpty { EnhancedPromptNote(text: enhanced) }
                }
                if let name = message.video {
                    InlineVideo(url: History.videoURL(name, in: conversation), onFull: onOpenVideo)
                }
                if let thought = message.thought, !thought.isEmpty {
                    DisclosureGroup {
                        Text(thought)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    } label: {
                        Label(message.text.isEmpty ? "Thinking…" : "Thought for \(Int((message.thoughtSeconds ?? 0).rounded())) s",
                              systemImage: "brain")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                    .padding(.horizontal, 10)
                }
                if !message.text.isEmpty {
                    if message.error == true {
                        VStack(alignment: .leading, spacing: 8) {
                            Label(message.text, systemImage: "exclamationmark.triangle.fill")
                                .font(.subheadline)
                                .foregroundStyle(.red)
                            if message.retry != nil {
                                Button("Retry", action: onRetry)
                                    .buttonStyle(.bordered)
                                    .tint(.red)
                                    .frame(minHeight: 44)
                            }
                            if message.queueLink == true {
                                Button("See what's running", action: onOpenQueue)
                                    .buttonStyle(.bordered)
                                    .frame(minHeight: 44)
                            }
                        }
                        .padding(10)
                        .background(Color.red.opacity(0.12))
                        .clipShape(RoundedRectangle(cornerRadius: Fx.bubbleRadius, style: .continuous))
                    } else {
                        Group {
                            if message.role == .fixer { ReplyText(text: message.text) } else { Text(message.text) }
                        }
                            .textSelection(.enabled)
                            .padding(10)
                            .padding(.horizontal, 4)
                            .background(message.role == .me ? Fx.bubbleMe : Fx.bubbleFixer)
                            .clipShape(RoundedRectangle(cornerRadius: Fx.bubbleRadius, style: .continuous))
                    }
                }
            }
            if message.role == .fixer { Spacer(minLength: 40) }
        }
    }
}

/// A reply drawn as formatted text (11-17: replies arrived as one wall with the ** showing): paragraphs with space
/// between them, headings, bullet and numbered lists with hanging indents, quotes, code blocks, and **bold**,
/// *italics*, `code` and links inside lines. Works while the reply is still streaming in.
struct ReplyText: View {
    let text: String

    enum Block: Hashable {
        case heading(String, Int), paragraph(String), bullet(String, Int), numbered(String, String, Int)
        case quote(String), code(String), rule
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(Self.blocks(text).enumerated()), id: \.offset) { _, block in
                view(block)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder private func view(_ block: Block) -> some View {
        switch block {
        case .heading(let s, let level):
            Text(Self.inline(s)).font(level <= 1 ? .title3.bold() : level == 2 ? .headline : .subheadline.bold())
                .padding(.top, 4)
        case .paragraph(let s):
            Text(Self.inline(s))
        case .bullet(let s, let depth):
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(depth == 0 ? "•" : "◦").foregroundStyle(.secondary)
                Text(Self.inline(s))
            }
            .padding(.leading, CGFloat(depth) * 16)
        case .numbered(let n, let s, let depth):
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(n + ".").monospacedDigit().foregroundStyle(.secondary)
                Text(Self.inline(s))
            }
            .padding(.leading, CGFloat(depth) * 16)
        case .quote(let s):
            Text(Self.inline(s)).foregroundStyle(.secondary).italic()
                .padding(.leading, 10)
                .overlay(alignment: .leading) { Rectangle().fill(Color.secondary.opacity(0.4)).frame(width: 3) }
        case .code(let s):
            ScrollView(.horizontal, showsIndicators: false) {
                Text(s).font(.system(.footnote, design: .monospaced)).padding(8)
            }
            .background(Color(.tertiarySystemBackground))
            .clipShape(RoundedRectangle(cornerRadius: 8))
        case .rule:
            Divider()
        }
    }

    static func inline(_ s: String) -> AttributedString {
        let opts = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: s, options: opts)) ?? AttributedString(s)
    }

    static func blocks(_ text: String) -> [Block] {
        var out: [Block] = []
        var para: [String] = []
        var code: [String]? = nil
        func flush() {
            if !para.isEmpty { out.append(.paragraph(para.joined(separator: "\n"))); para = [] }
        }
        for raw in text.components(separatedBy: "\n") {
            let line = raw.replacingOccurrences(of: "\t", with: "    ")
            let t = line.trimmingCharacters(in: .whitespaces)
            if t.hasPrefix("```") {
                if let c = code { out.append(.code(c.joined(separator: "\n"))); code = nil } else { flush(); code = [] }
                continue
            }
            if code != nil { code?.append(line); continue }
            let depth = min(3, (line.prefix { $0 == " " }.count) / 2)
            if t.isEmpty { flush(); continue }
            if t.hasPrefix("#") {
                let level = t.prefix { $0 == "#" }.count
                let body = t.dropFirst(level).trimmingCharacters(in: .whitespaces)
                if !body.isEmpty, level <= 6 { flush(); out.append(.heading(body, level)); continue }
            }
            if t == "---" || t == "***" || t == "___" { flush(); out.append(.rule); continue }
            if t.hasPrefix("> ") { flush(); out.append(.quote(String(t.dropFirst(2)))); continue }
            if let first = t.first, "-*•+".contains(first), t.dropFirst().first == " " {
                flush(); out.append(.bullet(t.dropFirst(2).trimmingCharacters(in: .whitespaces), depth)); continue
            }
            let digits = t.prefix { $0.isNumber }
            if !digits.isEmpty, digits.count <= 3 {
                let rest = t.dropFirst(digits.count)
                if let m = rest.first, m == "." || m == ")", rest.dropFirst().first == " " {
                    flush()
                    out.append(.numbered(String(digits), rest.dropFirst(2).trimmingCharacters(in: .whitespaces), depth))
                    continue
                }
            }
            para.append(t)
        }
        if let c = code { out.append(.code(c.joined(separator: "\n"))) }   // still streaming: show it anyway
        flush()
        return out
    }
}

struct HistorySheet: View {
    @ObservedObject var chat: Chat
    var onCleared: () -> Void = {}
    @Environment(\.dismiss) private var dismiss
    @State private var items: [Conversation] = []
    @State private var confirmClear = false
    @State private var clearing = false

    var body: some View {
        NavigationStack {
            List {
                if items.isEmpty {
                    Text("No saved conversations yet.").foregroundStyle(.secondary)
                }
                ForEach(items) { c in
                    Button {
                        chat.open(c)
                        dismiss()
                    } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(c.title).lineLimit(1)
                            Text(c.updated.formatted(date: .abbreviated, time: .shortened)).font(.footnote).foregroundStyle(.secondary)
                        }
                    }
                }
                .onDelete { idx in
                    for i in idx { History.delete(items[i].id) }
                    items.remove(atOffsets: idx)
                }
                Section {
                    Button(role: .destructive) { confirmClear = true } label: {
                        Label("Clear all", systemImage: "trash").frame(maxWidth: .infinity, minHeight: 44)
                    }
                } footer: {
                    Text("Erases every chat, picture and video on this iPhone and anything your computer still holds. "
                         + "Protected Library items stay. You'll see each part ticked off.")
                }
            }
            .confirmationDialog("Clear all?", isPresented: $confirmClear, titleVisibility: .visible) {
                Button("Clear all", role: .destructive) { clearing = true }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Every chat, picture and video on this iPhone and your computer is erased. Protected Library items stay. This can't be undone.")
            }
            .navigationDestination(isPresented: $clearing) {
                ClearEverythingView(chat: chat, onClear: onCleared, onFinished: { dismiss() }, startNow: true)
            }
            .navigationTitle("History")
            .toolbar {
                ToolbarItem(placement: .topBarLeading) { Button("New chat") { chat.newChat(); dismiss() } }
                ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() } }
            }
            .onAppear { chat.save(); items = History.list() }
        }
    }
}

struct ToolChip: View {
    let title: String
    let icon: String
    let on: Bool
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            Label(title, systemImage: icon)
                .font(.subheadline)
                .padding(.horizontal, 12).padding(.vertical, 7)
                .background(on ? Color.accentColor.opacity(0.3) : Color(.secondarySystemBackground))
                .clipShape(Capsule())
        }
        .buttonStyle(.plain)
    }
}

/// Download/loading state of the on-device chat model, when it isn't simply ready.
struct BrainStatusBar: View {
    @ObservedObject private var brain = Brain.shared
    var body: some View {
        switch brain.state {
        case .downloading(let p):
            VStack(alignment: .leading, spacing: 4) {
                Text("Downloading chat model… \(Int(p * 100))%").font(.footnote)
                ProgressView(value: p)
            }.padding(.horizontal).padding(.vertical, 6)
        case .loading:
            HStack { ProgressView(); Text("Loading chat model…").font(.footnote) }.padding(6)
        case .failed(let msg):
            Text("The chat model couldn't load: \(msg)").font(.footnote).foregroundStyle(.red).padding(6)
        default:
            EmptyView()
        }
    }
}

extension View {
    /// Icon-only buttons get a 44 pt tap area.
    func iconButton() -> some View {
        self.frame(minWidth: 44, minHeight: 44).contentShape(Rectangle())
    }
}
