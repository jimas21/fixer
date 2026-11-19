import PhotosUI
import SwiftUI

/// The bodies of Make's control sections (the photo, style tips, add-ons, the model's settings, video and change
/// help), plus `drawerSection`, the card they sit in. The compose sheet (MakeCompose.swift) lays these out inline;
/// they used to be the pages of a settings drawer.
extension CreateScreen {

    func drawerSection<C: View>(_ title: String?, @ViewBuilder _ content: () -> C) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if let title {
                Text(title).font(.footnote.weight(.semibold)).foregroundStyle(Fx.secondaryText)
            }
            content()
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Fx.tile))
    }

    // MARK: picture

    @ViewBuilder
    func pictureSettingsPage(_ r: CreateRecipe) -> some View {
        let settings = r.controls.filter { !$0.advanced && !["prompt", "shape", "count"].contains($0.key) }
        if !settings.isEmpty {
            drawerSection(nil) {
                ForEach(settings) { c in
                    if c.kind == "text" {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(c.label).font(.footnote).foregroundStyle(Fx.secondaryText)
                            TextField(c.label, text: Binding(get: { texts[c.key] ?? "" }, set: { texts[c.key] = $0 }),
                                      axis: .vertical)
                                .lineLimit(2...5)
                        }
                    } else {
                        numberOrMenu(c)
                    }
                }
            }
        }
        recipeWords
        drawerSection(nil) { advancedSection(r) }
        if let note = r.note {
            drawerSection("About this model") {
                Text(note).font(.footnote).foregroundStyle(Fx.secondaryText)
            }
        }
    }

    @ViewBuilder var photoSection: some View {
        if let photo {
            HStack(alignment: .top, spacing: 12) {
                Image(uiImage: photo).resizable().scaledToFill().frame(width: 72, height: 72).clipped()
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                Picker("Use the photo", selection: $photoMode) {
                    Text("Edit it").tag("edit")
                    Text("Start from it").tag("start")
                    Text("Reference").tag("reference")
                }
                .pickerStyle(.menu)
                Spacer()
                Button { self.photo = nil; morePhotos = [] } label: {
                    Image(systemName: "xmark.circle.fill").foregroundStyle(Fx.secondaryText).iconButton()
                }
                .buttonStyle(.borderless)
                .accessibilityLabel("Remove the photo")
            }
            photoModelPicker
            if photoMode != "start" {
                // 11-17: "use this from this one, that from that one": up to three photos, called photo 1, 2, 3.
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 10) {
                        numbered(photo, 1, remove: nil)
                        ForEach(Array(morePhotos.enumerated()), id: \.offset) { i, ui in
                            numbered(ui, i + 2) { morePhotos.remove(at: i) }
                        }
                        if morePhotos.count < 2 {
                            PhotosPicker(selection: $morePick, matching: .images) {
                                VStack(spacing: 4) {
                                    Image(systemName: "plus").font(.title3)
                                    Text("Photo \(morePhotos.count + 2)").font(.caption2)
                                }
                                .frame(width: 64, height: 64)
                                .background(RoundedRectangle(cornerRadius: 8)
                                    .strokeBorder(.secondary.opacity(0.5), style: StrokeStyle(lineWidth: 1, dash: [4])))
                            }
                            .accessibilityLabel("Add another photo")
                        }
                    }
                }
                if !morePhotos.isEmpty {
                    Text("Combining \(1 + morePhotos.count) photos. Say which part comes from which: “Put the shirt from photo 2 on the person in photo 1, in the pose from photo 3. Keep the face from photo 1.”")
                        .font(.caption).foregroundStyle(Fx.secondaryText)
                }
            }
            if photoMode == "start" {
                VStack(alignment: .leading, spacing: 2) {
                    HStack {
                        Text("How much to change")
                        Spacer()
                        Text("\(Int(strength * 100))%").monospacedDigit().foregroundStyle(Fx.secondaryText)
                    }
                    Slider(value: $strength, in: 0.2...1.0, step: 0.05)
                    HStack {
                        Text("← Keep more of the photo")
                        Spacer()
                        Text("Change more →")
                    }
                    .font(.caption2).foregroundStyle(Fx.secondaryText)
                }
            }
            Text(photoHelp).font(.caption).foregroundStyle(Fx.secondaryText)
        } else {
            PhotosPicker(selection: $photoPicks, maxSelectionCount: 3, matching: .images) {
                Label("Add photos", systemImage: "photo.badge.plus").frame(minHeight: 44)
            }
            Text("Add one to edit it, redraw it, or borrow a person, outfit or style from it. Add two or three to "
                 + "combine them into one scene — then say which part comes from which.")
                .font(.caption).foregroundStyle(Fx.secondaryText)
        }
    }

    /// The style tips, right on the page: a row of tap-on / tap-off phrasings per group.
    func styleTips(_ r: CreateRecipe) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            ForEach(r.toggles) { g in
                if !g.name.isEmpty {
                    Text(g.name).font(.caption).foregroundStyle(Fx.secondaryText).padding(.top, 4)
                }
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 6) {
                        ForEach(g.items) { t in tipChip(t) }
                    }
                    .padding(.trailing, 12)
                }
                .padding(.trailing, -12)
            }
            HStack(alignment: .center, spacing: 8) {
                Text("Tried-and-true phrasings, worded for this model, added to your prompt when you create.")
                    .font(.caption).foregroundStyle(Fx.secondaryText)
                Spacer(minLength: 0)
                Button("Clear") { chosen.removeAll() }
                    .frame(minHeight: 44)
                    .disabled(chosen.isEmpty)
            }
            .padding(.top, 4)
        }
    }

    private func tipChip(_ t: PromptToggle) -> some View {
        let on = chosen.contains(t.id)
        return Button {
            if on { chosen.remove(t.id) } else { chosen.insert(t.id) }
            quickTick += 1
        } label: {
            Text(t.label)
                .font(.system(size: 14, weight: on ? .semibold : .regular))
                .lineLimit(1)
                .foregroundStyle(on ? styleTint : Color.primary)
                .padding(.horizontal, 11)
                .frame(height: 32)
                .background(Capsule().fill(on ? styleFill : Fx.tile))
                .overlay(Capsule().strokeBorder(on ? styleStroke : Color.clear, lineWidth: 1))
                .frame(minHeight: 44)
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(on ? .isSelected : [])
    }

    // MARK: add-ons (LoRAs)

    /// Which LoRAs fit what the dock would make right now: the video model, the photo editor, or the picture model.
    var loraFamily: String {
        if mode == "video" { return videoLoraFamily }
        if photo != nil, photoMode != "start" { return editor == "qwen21" ? "qwen21" : "qwen-edit" }
        return recipe?.loraFamily ?? ""
    }

    var loraModelName: String {
        if mode == "video" { return "Wan 2.2 video" }
        if photo != nil, photoMode != "start" { return editor == "qwen21" ? "Qwen-Image 2.1" : "Qwen Edit" }
        return recipe?.name ?? "this model"
    }

    /// The add-ons on the computer that fit it. One the computer couldn't sort is offered everywhere.
    var fittingLoras: [LoraEntry] {
        let family = loraFamily
        return loraLibrary.filter { $0.family == family || $0.family == "unknown" }
    }

    /// The add-ons switched on by hand: pictures and videos keep their own.
    private var loraPicks: [LoraPick] { mode == "video" ? videoLoras : loras }

    private func setLoraPicks(_ picks: [LoraPick]) {
        if mode == "video" { videoLoras = picks } else { loras = picks }
    }

    /// What goes with the next run: the picks that fit. (The computer adds the "always" ones itself.)
    var activeLoras: [LoraPick] {
        let names = Set(fittingLoras.map(\.file))
        return loraPicks.filter { names.contains($0.name) }
    }

    func isLoraOn(_ e: LoraEntry) -> Bool {
        e.alwaysOn || loraPicks.contains { $0.name == e.file }
    }

    private func loraStrengthValue(_ e: LoraEntry) -> Double {
        loraPicks.first { $0.name == e.file }?.strength ?? e.strength
    }

    private func loraStrength(_ e: LoraEntry) -> Binding<Double> {
        Binding(get: { loraStrengthValue(e) },
                set: { v in
                    var picks = loraPicks
                    if let i = picks.firstIndex(where: { $0.name == e.file }) {
                        picks[i].strength = v
                    } else {
                        picks.append(LoraPick(name: e.file, strength: v))
                    }
                    setLoraPicks(picks)
                })
    }

    private func setLora(_ e: LoraEntry, on: Bool) {
        var picks = loraPicks
        picks.removeAll { $0.name == e.file }
        if on { picks.append(LoraPick(name: e.file, strength: e.strength)) }
        setLoraPicks(picks)
        if !on, e.alwaysOn { setLoraAlways(e, false) }
    }

    /// "Always use with this model": kept on the computer, so the chat's pictures and videos get it too.
    private func setLoraAlways(_ e: LoraEntry, _ on: Bool) {
        let level = loraStrengthValue(e)
        if on, !loraPicks.contains(where: { $0.name == e.file }) {
            setLoraPicks(loraPicks + [LoraPick(name: e.file, strength: level)])
        }
        if let i = loraLibrary.firstIndex(where: { $0.file == e.file }) { loraLibrary[i].alwaysOn = on }
        Task {
            do {
                let saved = try await Desktop.shared.loraSet(file: e.file, strength: level, alwaysOn: on)
                if let i = loraLibrary.firstIndex(where: { $0.file == saved.file }) { loraLibrary[i] = saved }
            } catch {
                if let i = loraLibrary.firstIndex(where: { $0.file == e.file }) { loraLibrary[i].alwaysOn = !on }
                styleNote = error.localizedDescription
            }
        }
    }

    /// An "always" add-on's strength is kept on the computer too, once the slider is let go.
    private func saveLoraStrength(_ e: LoraEntry) {
        let level = loraStrengthValue(e)
        Task {
            if let saved = try? await Desktop.shared.loraSet(file: e.file, strength: level),
               let i = loraLibrary.firstIndex(where: { $0.file == saved.file }) {
                loraLibrary[i] = saved
            }
        }
    }

    /// A LoRA just added in the browser: listed at once, and switched on when it fits what is being made.
    private func loraAdded(_ e: LoraEntry) {
        if let i = loraLibrary.firstIndex(where: { $0.file == e.file }) {
            loraLibrary[i] = e
        } else {
            loraLibrary.append(e)
        }
        if e.family == loraFamily || e.family == "unknown" { setLora(e, on: true) }
    }

    var loraSection: some View {
        let fitting = fittingLoras
        return VStack(alignment: .leading, spacing: 12) {
            if fitting.isEmpty {
                Text("No add-ons for \(loraModelName) on your computer yet.")
                    .font(.footnote).foregroundStyle(Fx.secondaryText)
            }
            ForEach(fitting) { e in
                SwipeToDelete(onDelete: { removingLora = e }) { loraRow(e) }
            }
            Button { showLoraBrowser = true } label: {
                Label("Add a LoRA", systemImage: "plus.circle").frame(minHeight: 44)
            }
            Text("Small add-ons that teach the model one extra look, subject or detail. Strength 1 is the add-on as "
                 + "made; lower blends it in gently, above 1.5 often distorts. Only add-ons made for "
                 + "\(loraModelName) are listed, because others would not work with it. Swipe a row (or press and "
                 + "hold it) to remove one from the computer.")
                .font(.caption).foregroundStyle(Fx.secondaryText)
        }
        .sheet(isPresented: $showLoraBrowser) {
            LoraBrowser(family: loraFamily, modelName: loraModelName, onAdded: { e in loraAdded(e) })
        }
        .confirmationDialog("Remove \(removingLora?.name ?? "it") from your computer?",
                            isPresented: Binding(get: { removingLora != nil },
                                                 set: { if !$0 { removingLora = nil } }),
                            titleVisibility: .visible) {
            if let l = removingLora {
                Button("Remove from computer", role: .destructive) { removeLora(l) }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Frees its space on the computer. Adding it again later downloads it again.")
        }
    }

    /// Delete the add-on's file from the computer, and drop it from the picks and the library here.
    private func removeLora(_ e: LoraEntry) {
        loras.removeAll { $0.name == e.file }
        videoLoras.removeAll { $0.name == e.file }
        loraLibrary.removeAll { $0.file == e.file }
        Task {
            do { try await Desktop.shared.loraRemove(e.file) }
            catch {
                styleNote = error.localizedDescription                 // put it back if the computer refused
                if let fresh = try? await Desktop.shared.loraList() { loraLibrary = fresh.loras }
            }
        }
    }

    /// The activation words to send for an add-on: nil to use its own defaults, else its own words plus any the
    /// user typed into the "extra activation words" box (so the computer adds exactly these).
    func loraSendTriggers(_ file: String) -> [String]? {
        let extra = (loraExtra[file] ?? "")
            .split(whereSeparator: { $0 == "," || $0 == ";" || $0 == "\n" })
            .map { String($0).trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        guard !extra.isEmpty else { return nil }
        var all = loraLibrary.first { $0.file == file }?.triggers ?? []
        for w in extra where !all.contains(w) { all.append(w) }
        return all
    }

    /// One add-on: an on/off switch, and a chevron that drops down its own options — strength, the extra activation
    /// words some add-ons need, and "always use". One row is open at a time.
    private func loraRow(_ e: LoraEntry) -> some View {
        let expanded = expandedLora == e.file
        let level = loraStrength(e)
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Button {
                    withAnimation(.snappy(duration: 0.2)) { expandedLora = expanded ? nil : e.file }
                } label: {
                    HStack(spacing: 8) {
                        Image(systemName: "chevron.right")
                            .font(.caption.weight(.semibold)).foregroundStyle(Fx.secondaryText)
                            .rotationEffect(.degrees(expanded ? 90 : 0))
                        VStack(alignment: .leading, spacing: 2) {
                            Text(e.name).lineLimit(2)
                            if !e.triggers.isEmpty {
                                Text("Adds: " + e.triggers.joined(separator: ", "))
                                    .font(.caption).foregroundStyle(Fx.secondaryText).lineLimit(1)
                            }
                        }
                        Spacer(minLength: 0)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                Toggle("", isOn: Binding(get: { isLoraOn(e) }, set: { setLora(e, on: $0) })).labelsHidden()
            }
            .frame(minHeight: 44)
            if expanded {
                if isLoraOn(e) {
                    HStack {
                        Text("Strength").font(.footnote)
                        Slider(value: level, in: 0...2, step: 0.05, onEditingChanged: { editing in
                            if !editing, e.alwaysOn { saveLoraStrength(e) }
                        })
                        Text(String(format: "%.2f", level.wrappedValue))
                            .font(.footnote).monospacedDigit().foregroundStyle(Fx.secondaryText)
                    }
                } else {
                    Text("Turn it on to set its strength.").font(.caption).foregroundStyle(Fx.secondaryText)
                }
                VStack(alignment: .leading, spacing: 4) {
                    Text("Extra activation words").font(.footnote).foregroundStyle(Fx.secondaryText)
                    TextField("Words some add-ons need, comma-separated",
                              text: Binding(get: { loraExtra[e.file] ?? "" }, set: { loraExtra[e.file] = $0 }),
                              axis: .vertical)
                        .lineLimit(1...3)
                        .textFieldStyle(.roundedBorder)
                    Text(e.triggers.isEmpty
                         ? "This add-on lists no words of its own."
                         : "It already adds its own: " + e.triggers.joined(separator: ", "))
                        .font(.caption).foregroundStyle(Fx.secondaryText)
                }
                Toggle("Always use with this model", isOn: Binding(get: { e.alwaysOn }, set: { setLoraAlways(e, $0) }))
                    .font(.footnote)
            }
        }
        .contextMenu {
            Button(role: .destructive) { removingLora = e } label: {
                Label("Remove from computer", systemImage: "trash")
            }
        }
    }

    func advancedSection(_ r: CreateRecipe) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            DisclosureGroup("Advanced") {
                VStack(alignment: .leading, spacing: 12) {
                    ForEach(r.controls.filter { $0.advanced }) { c in advancedControl(c) }
                }
                .padding(.top, 8)
            }
            Text("Every setting starts at the recommended value for this model. Most pictures never need these.")
                .font(.caption).foregroundStyle(Fx.secondaryText)
            Button("Reset to recommended") {
                let prompt = texts["prompt"]
                numbers = [:]
                menus = [:]
                texts = prompt.map { ["prompt": $0] } ?? [:]
                fixedSeed = nil
            }
            .frame(minHeight: 44)
            .disabled(numbers.isEmpty && menus.isEmpty && fixedSeed == nil && texts.keys.allSatisfy { $0 == "prompt" })
        }
    }

    // MARK: video

    var videoPhotoPage: some View {
        drawerSection(nil) {
            if let videoPhoto {
                HStack(alignment: .top, spacing: 12) {
                    Image(uiImage: videoPhoto).resizable().scaledToFill().frame(width: 72, height: 72).clipped()
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                    Text("The video starts from this photo. Say what should move, or leave the words empty.")
                        .font(.footnote).foregroundStyle(Fx.secondaryText)
                    Spacer()
                    Button { self.videoPhoto = nil; videoPhotoID = nil } label: {
                        Image(systemName: "xmark.circle.fill").foregroundStyle(Fx.secondaryText).iconButton()
                    }
                    .buttonStyle(.borderless)
                    .accessibilityLabel("Remove the photo")
                }
            } else {
                Text("Start the video from a picture, or leave this empty and it is made from your words alone.")
                    .font(.footnote).foregroundStyle(Fx.secondaryText)
            }
            let stills = sessionStills
            if !stills.isEmpty {
                Text("Made this session").font(.footnote.weight(.semibold)).foregroundStyle(Fx.secondaryText)
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(stills.reversed()) { r in sessionStill(r) }
                    }
                    .padding(.trailing, 12)
                }
                .padding(.trailing, -12)
            }
            PhotosPicker(selection: $videoPhotoPick, matching: .images) {
                Label("Pick from Photos", systemImage: "photo.badge.plus").frame(minHeight: 44)
            }
        }
    }

    /// One of this session's pictures, to start the video from. The one in use is outlined.
    private func sessionStill(_ r: CreateResult) -> some View {
        let on = videoPhoto != nil && r.libraryID != nil && r.libraryID == videoPhotoID
        return Button { useForVideo(r) } label: {
            Image(uiImage: r.image)
                .resizable()
                .scaledToFill()
                .frame(width: 64, height: 64)
                .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                .overlay {
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .strokeBorder(Fx.accent, lineWidth: on ? 2 : 0)
                }
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Picture made this session")
        .accessibilityHint("Starts the video from it")
        .accessibilityAddTraits(on ? .isSelected : [])
    }

    @ViewBuilder var videoSettingsPage: some View {
        drawerSection("Length") {
            Picker("Length", selection: $videoSeconds) {
                Text("5 s").tag(5)
                Text("10 s").tag(10)
                Text("20 s").tag(20)
                Text("30 s").tag(30)
            }
            .pickerStyle(.segmented)
            Text("Longer videos are made five seconds at a time, each part continuing from the last, so a 30-second video takes about six times as long as a 5-second one.")
                .font(.caption).foregroundStyle(Fx.secondaryText)
        }
        drawerSection("Explicit videos") {
            Picker("Explicit videos", selection: $rules.explicitVideo) {
                Text("Off").tag("off")
                Text("On").tag("on")
                Text("Auto").tag("auto")
            }
            .pickerStyle(.segmented)
            Text("Off never uses the explicit add-on. On always does. Auto decides from your words. This is for videos made here; the chat always decides by your words.")
                .font(.caption).foregroundStyle(Fx.secondaryText)
        }
        recipeWords
        drawerSection(nil) {
            Text("Made on your computer by a video model that turns words, or a photo, into a clip with natural motion. A 5-second clip takes about 2–3 minutes; longer ones take proportionally longer.")
                .font(.footnote).foregroundStyle(Fx.secondaryText)
        }
    }

    // MARK: shared

    /// The recipe's closing words, added after the owner's own.
    @ViewBuilder var recipeWords: some View {
        if effectiveTarget == .new, mode == "video", !videoSuffix.isEmpty {
            drawerSection("Recipe words at the end") {
                TextField("", text: $videoSuffix, axis: .vertical).foregroundStyle(styleTint)
                Text("Added after your words.").font(.caption).foregroundStyle(Fx.secondaryText)
            }
        } else if effectiveTarget == .new, mode == "picture", !styleSuffix.isEmpty {
            drawerSection("Recipe words at the end") {
                TextField("", text: $styleSuffix, axis: .vertical).foregroundStyle(styleTint)
                Text("Added after your words.").font(.caption).foregroundStyle(Fx.secondaryText)
            }
        }
    }

    /// Changing the picture on the canvas: what the two ways do, and what the chosen editor is good at.
    @ViewBuilder var helpPage: some View {
        drawerSection(nil) {
            Text(changeMode == "edit"
                 ? "Keep the rest: only what you ask for changes."
                 : "Redraw it: the picture's own model draws it again with your change; the layout mostly stays.")
                .font(.footnote).foregroundStyle(Fx.secondaryText)
        }
        if canChange { drawerSection("How changes work") { changeHelp } }
    }

    var changeHelp: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(editorHelp).font(.caption).foregroundStyle(Fx.secondaryText)
            if changeMode == "edit", editor == "qwen21" { explicitAddonPicker }
        }
    }
}

/// Swipe a row left to delete it. The add-ons card is a plain stack, not a List, so SwiftUI's own
/// `.swipeActions` isn't available here. Drag left past halfway and it asks to remove the add-on, then the row
/// springs back (the confirmation dialog does the rest). `.gesture` is low priority, so a drag that lands on the
/// strength slider stays with the slider — swipe from the name. Long-press still offers Remove too.
private struct SwipeToDelete<Content: View>: View {
    var onDelete: () -> Void
    @ViewBuilder var content: () -> Content
    @State private var offset: CGFloat = 0
    private let reveal: CGFloat = 96

    var body: some View {
        ZStack(alignment: .trailing) {
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(.red)
                .overlay(alignment: .trailing) {
                    Image(systemName: "trash")
                        .foregroundStyle(.white)
                        .frame(width: reveal)
                        .opacity(offset < -8 ? 1 : 0)
                }
            content()
                .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Fx.tile))
                .offset(x: offset)
                .gesture(
                    DragGesture(minimumDistance: 24)
                        .onChanged { v in
                            guard abs(v.translation.width) > abs(v.translation.height) else { return }
                            offset = max(-reveal, min(0, v.translation.width))
                        }
                        .onEnded { v in
                            if v.translation.width < -reveal / 2 { onDelete() }
                            withAnimation(.snappy(duration: 0.2)) { offset = 0 }
                        }
                )
        }
    }
}
