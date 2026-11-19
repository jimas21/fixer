import PhotosUI
import SwiftUI

/// The compose-sheet building blocks: the recipe-chip row, the prompt box, the tile rows and the one wide Make
/// button (MakeCompose.swift arranges them). The explicit chips are coloured per chip here.
extension CreateScreen {
    /// The prompt box is two lines at rest, so the picture keeps the room, and grows while it is being typed in.
    var wordLines: ClosedRange<Int> { focus == .words ? 3...6 : 2...3 }

    // MARK: recipe row

    var hasRecipeRow: Bool {
        canChange || (effectiveTarget == .new && (!visibleStyles.isEmpty || applied != nil || (fixedSeed != nil && mode == "picture")))
    }

    var recipeRow: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                if canChange { targetSwitch }
                if effectiveTarget == .new {
                    if styleIsCustom { customChip }
                    ForEach(visibleStyles) { s in styleChip(s) }
                    if mode == "picture", let seed = fixedSeed { seedChip(seed) }
                } else if let w = working {
                    let list = store.results
                    let index = (list.firstIndex(where: { $0.id == w.id }) ?? list.count - 1) + 1
                    Text("Changing picture \(index) of \(list.count)")
                        .font(.footnote)
                        .foregroundStyle(Fx.secondaryText)
                        .padding(.horizontal, 6)
                }
            }
            .padding(.trailing, 12)
        }
        .padding(.trailing, -12)
        .frame(minHeight: 44)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(makeExplicit ? "Explicit recipe" : "Recipe")
    }

    var targetSwitch: some View {
        HStack(spacing: 0) {
            targetSegment("New", .new)
            targetSegment("Change this", .change)
        }
        .padding(3)
        .frame(height: 38)
        .background(Capsule().fill(Fx.tile))
        .fxAnimation(Motion.pop, value: target)
    }

    private func targetSegment(_ title: String, _ value: MakeTarget) -> some View {
        Button { target = value } label: {
            Text(title)
                .font(.footnote.weight(.semibold))
                .foregroundStyle(effectiveTarget == value ? Color.primary : Fx.secondaryText)
                .padding(.horizontal, 12)
                .frame(maxHeight: .infinity)
                .background { if effectiveTarget == value { Capsule().fill(Fx.segment) } }
                .frame(minHeight: 44)
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(effectiveTarget == value ? .isSelected : [])
    }

    // The everyday (accent) colours. Explicit chips and explicit quick picks get their own warm colours, per chip,
    // so when "Explicit options in Make" is on they read as a distinct kind among the everyday ones (11-18).
    var styleTint: Color { Fx.accentSoftText }
    var styleFill: Color { Fx.onTintStrong }
    var styleStroke: Color { Fx.accent }

    private func recipeChipBody(_ title: String, on: Bool, check: Bool, explicit: Bool = false) -> some View {
        let tint = explicit ? Fx.explicitText : Fx.accentSoftText
        let fill = explicit ? Fx.explicitFill : Fx.onTintStrong
        let stroke = explicit ? Fx.explicitStroke : Fx.accent
        // An explicit chip keeps a faint warm outline even unselected, so it is recognisable in the row.
        let idleStroke = explicit ? Fx.explicitStroke.opacity(0.5) : Color.clear
        return HStack(spacing: 5) {
            if check { Image(systemName: "checkmark").font(.system(size: 12, weight: .bold)) }
            Text(title).font(.system(size: 14, weight: .semibold)).lineLimit(1)
        }
        .foregroundStyle(on ? tint : Color.primary)
        .padding(.horizontal, 13)
        .frame(height: 34)
        .background(Capsule().fill(on ? fill : Fx.tile))
        .overlay(Capsule().strokeBorder(on ? stroke : idleStroke, lineWidth: 1))
        .frame(minHeight: 44)
        .contentShape(Capsule())
    }

    private func styleChip(_ s: StyleRecipe) -> some View {
        let on = applied?.id == s.id && !styleIsCustom
        return Button { applyStyle(s) } label: { recipeChipBody(s.name, on: on, check: on, explicit: s.explicit) }
            .buttonStyle(.plain)
            .accessibilityAddTraits(on ? .isSelected : [])
            .accessibilityHint(s.explicit ? "Explicit. \(s.note ?? "")" : (s.note ?? ""))
    }

    private var customChip: some View {
        recipeChipBody("Custom", on: true, check: false)
            .accessibilityLabel("Custom")
            .accessibilityHint("Settings were changed by hand after the recipe")
            .accessibilityAddTraits(.isSelected)
    }

    private func seedChip(_ seed: Int) -> some View {
        Button { fixedSeed = nil } label: {
            HStack(spacing: 5) {
                Text("Seed \(String(seed))").font(.system(size: 14, weight: .semibold)).lineLimit(1)
                Image(systemName: "xmark").font(.system(size: 11, weight: .bold))
            }
            .foregroundStyle(Color.primary)
            .padding(.horizontal, 13)
            .frame(height: 34)
            .background(Capsule().fill(Fx.tile))
            .frame(minHeight: 44)
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Same seed \(String(seed))")
        .accessibilityHint("Use a random seed again")
    }

    // MARK: prompt box

    var promptBinding: Binding<String> {
        Binding(get: { texts["prompt"] ?? "" }, set: { texts["prompt"] = $0 })
    }

    var promptBox: some View {
        HStack(alignment: .top, spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                if effectiveTarget == .change {
                    TextField("Change something: “brighter”, “add a hat”…", text: $changeText, axis: .vertical)
                        .lineLimit(2...5)
                        .submitLabel(.go)
                        .focused($focus, equals: .change)
                        .onSubmit { if let w = working { startChange(w) } }
                } else if mode == "video" {
                    if !videoPrefix.isEmpty {
                        TextField("", text: $videoPrefix, axis: .vertical)
                            .font(.system(size: 16))
                            .foregroundStyle(styleTint)
                            .focused($focus, equals: .prefix)
                    }
                    TextField("What should the video show?", text: $videoPrompt, axis: .vertical)
                        .lineLimit(wordLines)
                        .focused($focus, equals: .words)
                } else {
                    if !stylePrefix.isEmpty {
                        TextField("", text: $stylePrefix, axis: .vertical)
                            .font(.system(size: 16))
                            .foregroundStyle(styleTint)
                            .focused($focus, equals: .prefix)
                    }
                    TextField("What should the picture show?", text: promptBinding, axis: .vertical)
                        .lineLimit(wordLines)
                        .focused($focus, equals: .words)
                }
            }
            .font(.body)
            .frame(maxWidth: .infinity, alignment: .leading)
            // The photo lives in the Photo card below the words (compose layout, 11-18), where one or several can be
            // added at once; no separate photo button here.
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: Fx.promptRadius, style: .continuous).fill(Fx.fieldWell))
        .overlay(RoundedRectangle(cornerRadius: Fx.promptRadius, style: .continuous).strokeBorder(Fx.wellStroke, lineWidth: 0.5))
    }

    // MARK: quick picks

    func quickRow(_ g: QuickGroup) -> some View {
        let options = g.options + (makeExplicit ? g.explicitOptions : [])
        let explicitSet = Set(g.explicitOptions)
        let text = quickBinding.wrappedValue
        return HStack(spacing: 0) {
            Text(g.label)
                .font(.footnote)
                .foregroundStyle(Fx.secondaryText)
                .lineLimit(1)
                .frame(width: 66, alignment: .leading)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(options, id: \.self) { o in
                        let on = PhraseList.contains(text, o)
                        let exp = explicitSet.contains(o)
                        let tint = exp ? Fx.explicitText : styleTint
                        let fill = exp ? Fx.explicitFill : styleFill
                        let stroke = exp ? Fx.explicitStroke : styleStroke
                        let idleStroke = exp ? Fx.explicitStroke.opacity(0.4) : Color.clear
                        Button { tapQuick(o) } label: {
                            Text(o)
                                .font(.system(size: 14, weight: on ? .semibold : .regular))
                                .lineLimit(1)
                                .foregroundStyle(on ? tint : Color.primary)
                                .padding(.horizontal, 11)
                                .frame(height: 32)
                                .background(Capsule().fill(on ? fill : Fx.tile))
                                .overlay(Capsule().strokeBorder(on ? stroke : idleStroke, lineWidth: 1))
                                .frame(minHeight: 44)
                                .contentShape(Capsule())
                        }
                        .buttonStyle(.plain)
                        .accessibilityAddTraits(on ? .isSelected : [])
                        .accessibilityHint(exp ? "Explicit" : "")
                    }
                }
                .padding(.trailing, 12)
            }
            .padding(.trailing, -12)
        }
        .accessibilityElement(children: .contain)
    }

    // MARK: tiles

    func pictureTiles(_ r: CreateRecipe) -> some View {
        let enhanceOn = promptEnhance.wrappedValue
        return TileRow(weights: [1.6, 1, 1, 1]) {
            Button { if recipes.count > 1 { showModels = true } } label: {
                MakeTile(caption: "Model", value: r.name)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Model: \(r.name)")
            .accessibilityHint(recipes.count > 1 ? "Opens the list of models" : "")
            Menu {
                Picker("Shape", selection: $shape) {
                    ForEach(shapeOptions, id: \.self) { o in Text(o.capitalized).tag(o) }
                }
            } label: { MakeTile(caption: "Shape", value: shape.capitalized) }
                .accessibilityLabel("Shape: \(shape)")
            Menu {
                Picker("How many", selection: pictureCount) {
                    ForEach(Array(countRange), id: \.self) { n in Text(String(n)).tag(n) }
                }
            } label: { MakeTile(caption: "Pictures", value: String(pictureCount.wrappedValue)) }
                .accessibilityLabel("Pictures: \(pictureCount.wrappedValue)")
            Button { promptEnhance.wrappedValue.toggle() } label: {
                MakeTile(caption: "Enhance", value: enhanceOn ? "On" : "Off", systemImage: "sparkles", on: enhanceOn)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Enhance my prompt")
            .accessibilityValue(enhanceOn ? "On" : "Off")
            .accessibilityHint(enhanceHint)
        }
    }

    var enhanceHint: String {
        promptEnhance.wrappedValue
            ? "Your computer adds detail to your words before drawing. Everything you asked for stays, exactly as explicit as you wrote it."
            : "Your words go to the picture model exactly as you wrote them."
    }

    var countRange: ClosedRange<Int> {
        let c = countControl
        let lo = Int(c?.min ?? 1), hi = Int(c?.max ?? 4)
        return lo...Swift.max(lo, hi)
    }

    var pictureCount: Binding<Int> {
        Binding(get: { Int(numbers["count"] ?? countControl?.defaultNumber ?? 1) },
                set: { numbers["count"] = Double($0) })
    }

    var videoTiles: some View {
        TileRow(weights: [1.6, 1, 1]) {
            MakeTile(caption: "Model", value: "Wan 2.2 14B")
                .accessibilityLabel("Video model: Wan 2.2 14B")
                .accessibilityHint("The explicit add-on is decided by your words")
            // The start photo comes from what was just made here as readily as from Photos: nothing has to be
            // saved to the phone first.
            Menu {
                if let w = working, w.videoURL == nil {
                    Button { useForVideo(w) } label: { Label("Use the picture showing", systemImage: "photo") }
                }
                // Picking from this session's pictures is on the video Photo card just below, which is always in view.
                Button { showVideoPhotos = true } label: {
                    Label("Pick from Photos", systemImage: "photo.on.rectangle")
                }
                if videoPhoto != nil {
                    Button(role: .destructive) { videoPhoto = nil; videoPhotoID = nil } label: {
                        Label("Remove the photo", systemImage: "xmark")
                    }
                }
            } label: {
                MakeTile(caption: "Photo", value: videoPhoto == nil ? "None" : "Added", systemImage: "photo")
            }
            .photosPicker(isPresented: $showVideoPhotos, selection: $videoPhotoPick, matching: .images)
            .accessibilityLabel("Start photo: \(videoPhoto == nil ? "none" : "added")")
            Menu {
                Picker("Explicit videos", selection: $rules.explicitVideo) {
                    Text("Off").tag("off")
                    Text("On").tag("on")
                    Text("Auto").tag("auto")
                }
            } label: { MakeTile(caption: "Explicit", value: rules.explicitVideo.capitalized) }
                .accessibilityLabel("Explicit videos: \(rules.explicitVideo)")
        }
    }

    var changeTiles: some View {
        let redraw = changeMode == "redraw"
        let enhanceOn = enhanceEdits
        return TileRow(weights: [1.6, 1, 1]) {
            Menu {
                Picker("Editor", selection: $editor) {
                    Text("Auto").tag("auto")
                    Text("Qwen Edit").tag("qwen")
                    Text("Qwen Edit Explicit").tag("qwen-explicit")
                    if hasQwen21 { Text("Qwen-Image 2.1").tag("qwen21") }
                }
                if editor == "qwen21" {
                    if addonOptions.count > 1 {
                        Picker("Explicit add-on", selection: Binding(get: { addonValue }, set: { explicitAddon = $0 })) {
                            ForEach(addonOptions, id: \.self) { Text($0).tag($0) }
                        }
                    } else {
                        Text("The explicit add-on isn't on your computer yet")
                    }
                }
            } label: {
                MakeTile(caption: "Editor", value: redraw ? "Its own model" : editorName)
            }
            .disabled(redraw)
            .accessibilityLabel("Editor: \(redraw ? "the picture's own model" : editorName)")
            .accessibilityHint(editorHelp)
            Menu {
                Picker("How", selection: $changeMode) {
                    Text("Keep the rest").tag("edit")
                    Text("Redraw it").tag("redraw")
                }
            } label: { MakeTile(caption: "How", value: redraw ? "Redraw it" : "Keep the rest") }
                .accessibilityLabel("How: \(redraw ? "Redraw it" : "Keep the rest")")
            Button { enhanceEdits.toggle() } label: {
                MakeTile(caption: "Enhance", value: enhanceOn ? "On" : "Off", systemImage: "sparkles", on: enhanceOn)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Enhance my prompt for changes")
            .accessibilityValue(enhanceOn ? "On" : "Off")
        }
    }

    var editorName: String {
        switch editor {
        case "qwen": "Qwen Edit"
        case "qwen-explicit": "Qwen Edit Explicit"
        case "qwen21": "Qwen-Image 2.1"
        default: "Auto"
        }
    }

    // MARK: the one wide button

    private enum MainStyle { case go, stop }

    private struct MainSpec {
        var title: String
        var systemImage: String
        var style: MainStyle
        var enabled: Bool
        var action: () -> Void
    }

    private var mainSpec: MainSpec {
        if effectiveTarget == .change, let w = working {
            let empty = changeText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            if !changeRunning {
                return MainSpec(title: "Change it", systemImage: "wand.and.rays", style: .go, enabled: !empty,
                                action: { startChange(w) })
            }
            if empty || changeFormSignature == reviseSignature {
                return MainSpec(title: "Stop changing", systemImage: "stop.fill", style: .stop, enabled: true,
                                action: { stopTick += 1; store.cancelChange() })
            }
            return MainSpec(title: "Change it instead", systemImage: "wand.and.rays", style: .go, enabled: true,
                            action: { startChange(w) })
        }
        if mode == "video" {
            let empty = videoPrompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && videoPhoto == nil
            if !store.videoRunning {
                return MainSpec(title: "Make video", systemImage: "film", style: .go, enabled: !empty,
                                action: { startVideoNow() })
            }
            if videoFormSignature == videoSignature {
                return MainSpec(title: "Stop video", systemImage: "stop.fill", style: .stop, enabled: true,
                                action: { stopTick += 1; store.cancelVideo() })
            }
            return MainSpec(title: "Make video instead", systemImage: "film", style: .go, enabled: !empty,
                            action: { startVideoNow() })
        }
        let empty = (texts["prompt"] ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        if !createRunning {
            return MainSpec(title: "Create", systemImage: "wand.and.stars", style: .go, enabled: !empty && recipe != nil,
                            action: { if let r = recipe { generate(r) } })
        }
        if formSignature == createSignature {
            return MainSpec(title: "Stop drawing", systemImage: "stop.fill", style: .stop, enabled: true,
                            action: { stopWhatRuns() })
        }
        return MainSpec(title: "Create instead", systemImage: "wand.and.stars", style: .go, enabled: !empty && recipe != nil,
                        action: { if let r = recipe { generate(r) } })
    }

    var mainButton: some View {
        let spec = mainSpec
        return Button(action: spec.action) {
            HStack(spacing: 8) {
                Image(systemName: spec.systemImage).font(.system(size: spec.style == .stop ? 12 : 16, weight: .semibold))
                Text(spec.title).font(.body.weight(.semibold))
            }
            .foregroundStyle(spec.style == .stop ? Color.primary : Fx.onSolid)
            .frame(maxWidth: .infinity, minHeight: 50)
            .background(Capsule().fill(spec.style == .stop ? Fx.solidSoft : Fx.solid))
            .opacity(spec.enabled ? 1 : 0.4)
            .contentShape(Capsule())
            .fxAnimation(Motion.fade, value: spec.title)
        }
        .buttonStyle(.plain)
        .disabled(!spec.enabled)
        .accessibilityLabel(spec.title)
    }

    func startChange(_ w: CreateResult) {
        reviseSignature = changeFormSignature
        startTick += 1
        applyChange(w)
    }

    func startVideoNow() {
        videoSignature = videoFormSignature
        startTick += 1
        Self.hideKeyboard()
        let style = applied?.kind == "video" ? applied?.id : nil
        store.startVideo(prompt: assembledVideoPrompt, start: videoPhoto, explicit: rules.explicitVideo,
                         parent: videoPhotoID, action: videoPhoto == nil ? "video" : "animate",
                         style: style,
                         loras: activeLoras.map { LoraSetting(name: $0.name, strength: $0.strength, triggers: loraSendTriggers($0.name)) },
                         seconds: videoSeconds)
    }
}

// MARK: - Tiles

/// One of the dock's small boxes: a caption over a value.
struct MakeTile: View {
    let caption: String
    let value: String
    var systemImage: String? = nil
    var on = false

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(caption).font(.caption2).foregroundStyle(on ? Fx.accentSoftText : Fx.secondaryText).lineLimit(1)
            HStack(spacing: 3) {
                if let systemImage { Image(systemName: systemImage).font(.system(size: 12, weight: .semibold)) }
                Text(value).font(.footnote.weight(.semibold)).lineLimit(1).minimumScaleFactor(0.8)
            }
            .foregroundStyle(on ? Fx.accentText : Color.primary)
        }
        .padding(8)
        .frame(maxWidth: .infinity, minHeight: 52, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: Fx.tileRadius, style: .continuous).fill(on ? Fx.onTint : Fx.tile))
        .contentShape(RoundedRectangle(cornerRadius: Fx.tileRadius, style: .continuous))
    }
}

/// Four boxes in a row, widths 1.6 : 1 : 1 : 1. At the biggest text sizes they become two by two.
struct TileRow<Content: View>: View {
    let weights: [CGFloat]
    let content: Content
    @Environment(\.dynamicTypeSize) private var dynamicType

    init(weights: [CGFloat], @ViewBuilder content: () -> Content) {
        self.weights = weights
        self.content = content()
    }

    var body: some View {
        if dynamicType >= .accessibility1 {
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 6), GridItem(.flexible(), spacing: 6)], spacing: 6) {
                content
            }
        } else {
            WeightedRow(weights: weights) { content }
        }
    }
}

/// Lays children side by side, dividing the width by `weights`.
struct WeightedRow: Layout {
    var weights: [CGFloat]
    var spacing: CGFloat = 6

    private func widths(_ total: CGFloat, count: Int) -> [CGFloat] {
        let w = Array(weights.prefix(count))
        let sum = w.reduce(0, +)
        guard sum > 0 else { return [] }
        let inner = total - spacing * CGFloat(Swift.max(0, count - 1))
        return w.map { inner * $0 / sum }
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let total = proposal.width ?? 320
        let ws = widths(total, count: subviews.count)
        var height: CGFloat = 0
        for (i, s) in subviews.enumerated() where i < ws.count {
            height = Swift.max(height, s.sizeThatFits(ProposedViewSize(width: ws[i], height: nil)).height)
        }
        return CGSize(width: total, height: height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let ws = widths(bounds.width, count: subviews.count)
        var x = bounds.minX
        for (i, s) in subviews.enumerated() where i < ws.count {
            s.place(at: CGPoint(x: x, y: bounds.minY), anchor: .topLeading,
                    proposal: ProposedViewSize(width: ws[i], height: bounds.height))
            x += ws[i] + spacing
        }
    }
}
