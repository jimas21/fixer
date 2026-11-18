import PhotosUI
import SwiftUI

/// Everything Make offers beyond the dock, shown when the panel is pulled all the way up: the photo, style tips,
/// add-ons, advanced settings, more quick picks and this session's pictures. The controls are the same ones that
/// were on the old form, with the same bindings.
extension CreateScreen {
    var makeDrawer: some View {
        ScrollView {
            VStack(spacing: 10) {
                if effectiveTarget == .change {
                    drawerSection(nil) {
                        Text(changeMode == "edit"
                             ? "Keep the rest: only what you ask for changes."
                             : "Redraw it: the picture's own model draws it again with your change; the layout mostly stays.")
                            .font(.footnote).foregroundStyle(Fx.secondaryText)
                    }
                    if canChange { drawerSection("How changes work") { changeHelp } }
                } else if mode == "video" {
                    videoDrawer
                } else if let r = recipe {
                    pictureDrawer(r)
                }
                moreQuickPicks
                recipeWords
                thisSession
            }
            .padding(.top, 2)
            .padding(.bottom, 4)
        }
        .scrollDismissesKeyboard(.interactively)
        .scrollIndicators(.hidden)
    }

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
    func pictureDrawer(_ r: CreateRecipe) -> some View {
        drawerSection("Photo") { photoSection }
        if let note = r.note {
            drawerSection("About this model") {
                Text(note).font(.footnote).foregroundStyle(Fx.secondaryText)
            }
        }
        let settings = r.controls.filter { !$0.advanced && !["prompt", "shape", "count"].contains($0.key) }
        if !settings.isEmpty {
            drawerSection("Settings") {
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
        if !r.toggles.isEmpty { drawerSection("Style tips") { styleTips(r) } }
        if !loraFiles.isEmpty { drawerSection("Style add-ons (LoRAs)") { loraSection } }
        drawerSection(nil) { advancedSection(r) }
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
                    Text("Say which part comes from which: “Put the shirt from photo 2 on the person in photo 1, in the pose from photo 3. Keep the face from photo 1.”")
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
            PhotosPicker(selection: $photoPick, matching: .images) {
                Label("Add a photo", systemImage: "photo.badge.plus").frame(minHeight: 44)
            }
        }
    }

    func styleTips(_ r: CreateRecipe) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Button { showHelpers = true } label: {
                Label(chosen.isEmpty ? "Style tips" : "Style tips (\(chosen.count) on)", systemImage: "checklist")
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
            }
            let on = r.toggles.flatMap(\.items).filter { chosen.contains($0.id) }
            if !on.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 6) {
                        ForEach(on) { t in
                            Button { chosen.remove(t.id) } label: {
                                HStack(spacing: 4) { Text(t.label); Image(systemName: "xmark").font(.caption2) }
                                    .font(.footnote).padding(.horizontal, 10)
                                    .frame(minHeight: 32)
                                    .background(Capsule().fill(Fx.onTint))
                                    .frame(minHeight: 44)
                                    .contentShape(Capsule())
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel("\(t.label), on")
                            .accessibilityHint("Turns it off")
                        }
                    }
                }
            }
            Text("Tried-and-true phrasings, worded for this model, added to your prompt when you create.")
                .font(.caption).foregroundStyle(Fx.secondaryText)
        }
    }

    var loraSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach($loras) { $pick in
                VStack(alignment: .leading) {
                    HStack {
                        Picker("LoRA", selection: $pick.name) { ForEach(loraFiles, id: \.self) { Text($0).tag($0) } }
                            .pickerStyle(.menu)
                        Spacer()
                        Button { let id = pick.id; loras.removeAll { $0.id == id } } label: {
                            Image(systemName: "minus.circle").foregroundStyle(Fx.danger).iconButton()
                        }
                        .buttonStyle(.borderless)
                        .accessibilityLabel("Remove this add-on")
                    }
                    HStack {
                        Text("Strength")
                        Slider(value: $pick.strength, in: 0...2, step: 0.05)
                        Text(String(format: "%.2f", pick.strength)).monospacedDigit().foregroundStyle(Fx.secondaryText)
                    }
                }
            }
            Button("Add a style add-on") { loras.append(LoraPick(name: loraFiles[0], strength: 1.0)) }
                .frame(minHeight: 44)
            Text("Small add-ons that teach the model one extra look, subject or detail. Strength 1 is the add-on as "
                 + "made; lower blends it in gently, above 1.5 often distorts. An add-on only works with the model "
                 + "family it was made for.")
                .font(.caption).foregroundStyle(Fx.secondaryText)
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

    @ViewBuilder var videoDrawer: some View {
        drawerSection("Start photo") {
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
                PhotosPicker(selection: $videoPhotoPick, matching: .images) {
                    Label("Animate a photo", systemImage: "photo.badge.plus").frame(minHeight: 44)
                }
            }
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
        drawerSection(nil) {
            Text("Made on your computer by a video model that turns words, or a photo, into a 5-second clip with natural motion. About 2–3 minutes.")
                .font(.footnote).foregroundStyle(Fx.secondaryText)
        }
    }

    // MARK: shared

    @ViewBuilder var moreQuickPicks: some View {
        let rest = Array(activeGroups.dropFirst(3))
        if recipeStore.catalog != nil, !rest.isEmpty {
            drawerSection("More quick picks") {
                VStack(spacing: 0) { ForEach(rest) { g in quickRow(g) } }
            }
        }
    }

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

    /// Everything made in Make this session, newest first. It lives in the app-level store, so it survives switching
    /// tabs; the Library also has every item.
    @ViewBuilder var thisSession: some View {
        if !store.results.isEmpty {
            drawerSection("This session") {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(store.results.reversed()) { res in
                            Image(uiImage: res.image).resizable().scaledToFill()
                                .frame(width: 72, height: 72).clipped()
                                .overlay {
                                    if res.videoURL != nil {
                                        Image(systemName: "play.circle.fill").font(.title2)
                                            .foregroundStyle(.white).shadow(radius: 3)
                                    }
                                }
                                .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                                .overlay {
                                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                                        .strokeBorder(Fx.accent, lineWidth: working?.id == res.id ? 2 : 0)
                                }
                                .contentShape(Rectangle())
                                .onTapGesture {
                                    workingID = res.id
                                    if res.videoURL == nil { target = .change }
                                    setDetent(.mid)
                                }
                                .onLongPressGesture { viewing = res }
                                .accessibilityElement()
                                .accessibilityLabel(res.videoURL != nil ? "Video" : "Picture")
                                .accessibilityAddTraits(.isButton)
                                .accessibilityAction(named: "Open full screen") { viewing = res }
                        }
                    }
                }
                Button { router.tab = .library } label: {
                    HStack { Text("All in Library"); Spacer(); Image(systemName: "chevron.right").font(.footnote) }
                        .frame(minHeight: 44)
                        .contentShape(Rectangle())
                }
            }
        }
    }

    var changeHelp: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(editorHelp).font(.caption).foregroundStyle(Fx.secondaryText)
            if changeMode == "edit", editor == "qwen21" { explicitAddonPicker }
        }
    }
}
