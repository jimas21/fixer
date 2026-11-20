import SwiftUI
import UIKit

/// Quick picks, grouped into collapsible sections (11-19: the flat "Quick picks" card listed every active group at
/// once; this buckets them under named headers that collapse by default and shrink away entirely when a kind of pick
/// isn't in the recipe). Each expanded row reuses the existing `quickRow` / `quickRowTiled`, so the chips themselves
/// are unchanged. Also here: the pictograph tile plumbing (Goal 2) — art names are placeholders for now.
extension CreateScreen {

    // MARK: sections

    /// The named sections quick-pick groups fall into, in display order. A group id in none of these lists lands in a
    /// trailing "More" section, so a new catalogue group is never dropped silently.
    static let quickSectionMap: [(name: String, ids: [String])] = [
        ("People",   ["subject", "heritage", "age"]),
        ("Body",     ["body", "body_size", "height", "fitness", "anatomy_f", "anatomy_m"]),
        ("Hair",     ["hair", "hair_color"]),
        ("Face",     ["features", "expression"]),
        ("Wardrobe", ["outfit", "props", "accessories"]),
        ("Scene",    ["setting", "lighting", "camera"]),
        ("Action",   ["pose", "act", "motion"]),
        ("Explicit", ["fluids", "reactions", "kink", "fetish"]),
    ]

    /// `activeGroups` bucketed into the named sections above. A section with no active group is dropped; each section
    /// keeps the group order given by the map; unmapped active groups fall into a trailing "More" section.
    var quickSections: [QuickSection] {
        let groups = activeGroups
        let byID = Dictionary(groups.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        var used = Set<String>()
        var out: [QuickSection] = []
        for section in Self.quickSectionMap {
            var gs: [QuickGroup] = []
            for id in section.ids {
                if let g = byID[id] { gs.append(g); used.insert(id) }
            }
            if !gs.isEmpty { out.append(QuickSection(name: section.name, groups: gs)) }
        }
        let more = groups.filter { !used.contains($0.id) }
        if !more.isEmpty { out.append(QuickSection(name: "More", groups: more)) }
        return out
    }

    /// How many of a group's options are switched on in the words the sheet is showing. Explicit options are counted
    /// only while Explicit is on, matching exactly what `quickRow` renders (reuses `PhraseList.contains`, the same
    /// membership test a chip uses to know it is active).
    func quickSelectedCount(_ g: QuickGroup) -> Int {
        let options = g.options + (makeExplicit ? g.explicitOptions : [])
        let text = quickBinding.wrappedValue
        return options.reduce(0) { $0 + (PhraseList.contains(text, $1) ? 1 : 0) }
    }

    /// Replaces the flat "Quick picks" card: one collapsible card per named section. Collapsed by default; each
    /// header shows a count of what's switched on; expanded rows reuse `quickRowTiled` (tiles where pictograph art
    /// exists, plain chips otherwise). 11-19: the style tips live here too, as their own collapsible sections after
    /// the quick picks, marked apart (wand header, dashed outline, sparkle chips) because they work differently:
    /// they never show in the words, the server adds them on Create, worded for the model. `tips` is empty where
    /// tips don't apply (Change, video).
    func quickSectionsCard(tips: [PromptToggleGroup]) -> some View {
        MakeSectionsCard(
            sections: quickSections,
            selectedNames: { quickSelectedNames($0) },
            row: { AnyView(quickRowTiled($0)) },
            tips: tips,
            tipNames: { g in g.items.filter { chosen.contains($0.id) }.map(\.label) },
            tipRow: { AnyView(tipRow($0)) },
            clearTips: chosen.isEmpty ? nil : { chosen.removeAll() },
            expanded: $quickExpanded
        )
        .fxAnimation(Motion.pop, value: makeExplicit)   // 11-19: the 18+ strips slide in / out, the rest shifts
    }

    /// 11-19: the picks switched on in a group, in the group's own order (everyday first, then 18+ while it's on).
    func quickSelectedNames(_ g: QuickGroup) -> [String] {
        let text = quickBinding.wrappedValue
        return (g.options + (makeExplicit ? g.explicitOptions : [])).filter { PhraseList.contains(text, $0) }
            .reduce(into: [String]()) { if !$0.contains($1) { $0.append($1) } }
    }

    /// 11-19: the "see all" sheet for one group (opened from its name tag).
    func quickPickSheet(_ g: QuickGroup) -> some View {
        let everydaySet = Set(g.options.map { $0.lowercased() })
        return QuickPickSheet(
            title: g.label,
            everyday: g.options,
            adult: makeExplicit ? g.explicitOptions.filter { !everydaySet.contains($0.lowercased()) } : [],
            isOn: { PhraseList.contains(quickBinding.wrappedValue, $0) },
            toggle: { tapQuick($0) }
        )
    }

    // MARK: tiled rows (Goal 2 plumbing)

    /// Like `quickRow`, but renders the group's options as pictograph tiles IF any visible option has art; otherwise
    /// it is exactly the plain chip row. Taps go through the same `tapQuick`, so selection logic is shared.
    @ViewBuilder
    func quickRowTiled(_ g: QuickGroup) -> some View {
        if g.options.contains(where: { Pictograph.asset(groupID: g.id, option: $0) != nil }) {
            // 11-19: tiles for the everyday options; 18+ ones (while on) get their own flame strip, as in quickRow.
            let everydaySet = Set(g.options.map { $0.lowercased() })
            let adult = makeExplicit ? g.explicitOptions.filter { !everydaySet.contains($0.lowercased()) } : []
            VStack(alignment: .leading, spacing: 0) {
                tiledGroup(g, options: g.options)
                if !adult.isEmpty {
                    HStack(spacing: 0) {
                        flameTag
                        chipStrip(adult, explicit: true)
                    }
                    .transition(.opacity.combined(with: .move(edge: .top)))
                }
            }
        } else {
            quickRow(g)
        }
    }

    /// The tile layout for one group: the group label over ONE horizontally scrolling strip of `PictographTile`s —
    /// the same shape as a chip row, so a 24-option group stays one row tall instead of a six-row grid. The warm
    /// explicit colours are applied per tile, the same way `quickRow` colours its explicit chips.
    private func tiledGroup(_ g: QuickGroup, options: [String]) -> some View {
        let explicitSet = Set(g.explicitOptions)
        let text = quickBinding.wrappedValue
        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Text(g.label).font(.footnote).foregroundStyle(Fx.secondaryText).lineLimit(1)
                Spacer(minLength: 8)
                Button { quickSheet = g } label: {                     // 11-19: the same "see all" as chip rows
                    HStack(spacing: 2) {
                        Image(systemName: "square.grid.2x2").font(.system(size: 9, weight: .semibold))
                        Text("all \(g.options.count + (makeExplicit ? g.explicitOptions.count : 0))")
                            .font(.system(size: 11))
                    }
                    .foregroundStyle(Fx.accentSoftText)
                    .frame(minHeight: 32)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("\(g.label): see all")
            }
            ScrollView(.horizontal, showsIndicators: false) {
                LazyHStack(spacing: 8) {
                    ForEach(options, id: \.self) { o in
                        PictographTile(
                            asset: Pictograph.asset(groupID: g.id, option: o),
                            label: o,
                            selected: PhraseList.contains(text, o),
                            explicit: explicitSet.contains(o)
                        ) { tapQuick(o) }
                    }
                }
                .padding(.trailing, 12)
            }
            .padding(.trailing, -12)
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .contain)
    }
}

// MARK: - The collapsible sections

/// One named section of quick-pick groups. Identified by its name (names are unique across `quickSectionMap` plus
/// the trailing "More"), which is also the key used for the per-section expand state.
struct QuickSection: Identifiable {
    let name: String
    let groups: [QuickGroup]
    var id: String { name }
}

/// The collapsible quick-pick sections. The expand state (a set of expanded section names) is owned by CreateScreen
/// (`quickExpanded`), NOT held here: `composeStack` identifies sections by offset, and that offset shifts when the
/// results roll appears or the recipe row comes and goes, which would reset a local @State and snap every section
/// shut. Everything a row draws comes in through closures, so this file never reimplements a chip.
struct MakeSectionsCard: View {
    let sections: [QuickSection]
    /// 11-19: what's picked in a group, shown under the section's name (a count alone said nothing).
    let selectedNames: (QuickGroup) -> [String]
    let row: (QuickGroup) -> AnyView
    /// The style-tip groups, each its own collapsible section after the quick picks (empty: none shown).
    let tips: [PromptToggleGroup]
    let tipNames: (PromptToggleGroup) -> [String]
    let tipRow: (PromptToggleGroup) -> AnyView
    /// Turns every tip off; nil when none is on.
    let clearTips: (() -> Void)?
    /// Collapsed by default: a section key is in here only after it has been opened (tip sections: "tip:<name>").
    @Binding var expanded: Set<String>

    var body: some View {
        VStack(spacing: 10) {
            ForEach(sections) { section in
                card(key: section.name, name: section.name,
                     picked: section.groups.flatMap { selectedNames($0) }, tip: false) {
                    AnyView(VStack(spacing: 0) { ForEach(section.groups) { g in row(g) } })
                }
            }
            if !tips.isEmpty {
                tipsLabel
                ForEach(Array(tips.enumerated()), id: \.offset) { i, g in
                    let name = g.name.isEmpty ? "Tips \(i + 1)" : g.name      // an unnamed group still gets a header
                    card(key: "tip:" + name, name: name, picked: tipNames(g), tip: true) { tipRow(g) }
                }
            }
        }
    }

    /// Sets the tips apart from the quick picks above them: what they are, and a way to turn them all off.
    private var tipsLabel: some View {
        HStack(spacing: 6) {
            Image(systemName: "wand.and.stars").font(.caption)
            Text("Style tips · added when you create, worded for this model")
                .font(.caption)
                .lineLimit(2)
            Spacer(minLength: 8)
            if let clearTips {
                Button("Clear", action: clearTips)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Fx.accent)
                    .frame(minHeight: 44)
            }
        }
        .foregroundStyle(Fx.secondaryText)
        .padding(.horizontal, 4)
        .padding(.top, 4)
    }

    private func card(key: String, name: String, picked: [String], tip: Bool,
                      content: () -> AnyView) -> some View {
        let count = picked.count
        let isOpen = expanded.contains(key)
        let rows: AnyView? = isOpen ? content() : nil
        return VStack(alignment: .leading, spacing: 8) {
            Button {
                if expanded.contains(key) { expanded.remove(key) } else { expanded.insert(key) }
            } label: {
                header(name: name, count: count, isOpen: isOpen, tip: tip)
            }
            .buttonStyle(.plain)
            .accessibilityLabel((tip ? "Style tips, " : "") + (count > 0 ? "\(name), \(count) selected" : name))
            .accessibilityHint(isOpen ? "Collapse" : "Expand")
            if !picked.isEmpty {
                // 11-19: what's on, always in view: no scrolling 40 chips to remember what was picked.
                Text(picked.joined(separator: " · "))
                    .font(.caption)
                    .foregroundStyle(tip ? Fx.secondaryText : Fx.accentSoftText)
                    .lineLimit(isOpen ? 3 : 2)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityHidden(true)               // the header already says how many
            }
            if let rows { rows }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Fx.tile))
        .overlay {
            if tip {
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .strokeBorder(Fx.hairline, style: StrokeStyle(lineWidth: 1, dash: [4, 4]))
            }
        }
        .fxAnimation(Motion.pop, value: isOpen)
    }

    private func header(name: String, count: Int, isOpen: Bool, tip: Bool) -> some View {
        HStack(spacing: 8) {
            if tip {
                Image(systemName: "wand.and.stars")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Fx.secondaryText)
            }
            Text(name).font(.footnote.weight(.semibold)).foregroundStyle(Fx.secondaryText)
            if count > 0 {
                Text(String(count))
                    .font(.caption2.weight(.bold))
                    .foregroundStyle(Fx.accentSoftText)
                    .frame(minWidth: 18, minHeight: 18)
                    .padding(.horizontal, 4)
                    .background(Capsule().fill(Fx.onTintStrong))
            }
            Spacer(minLength: 8)
            Image(systemName: "chevron.right")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(Fx.secondaryText)
                .rotationEffect(.degrees(isOpen ? 90 : 0))
        }
        .frame(minHeight: 32)
        .contentShape(Rectangle())
    }
}

// MARK: - Pictograph tile (Goal 2)

/// Looks up the pictograph art for a quick-pick option, by group id and option text. Returns nil when there is no
/// drawing for that option — the row then stays plain chips (see `quickRowTiled`).
///
/// The art and its `table` are GENERATED by `tools/pictographs.py`: it draws every icon (monochrome line art, template
/// images in Assets.xcassets/Pictographs, named "pg_<group>_<slug>") and writes `PictographTable.swift`. To add or
/// change a drawing, edit that script and re-run it; `--check` compares the drawings with the live catalogue
/// (recipes.json on the computer). A group with no entry in the table stays plain chips.
enum Pictograph {
    /// Option keys match the catalogue's option TEXT, case-insensitively (see `PictographTable.swift`).
    static func asset(groupID: String, option: String) -> String? {
        guard let group = table[groupID] else { return nil }
        if let exact = group[option] { return exact }
        let lower = option.lowercased()
        return group.first { $0.key.lowercased() == lower }?.value
    }
}

/// A selectable tile: a pictograph over its label, a fixed 76pt wide in the group's scrolling strip. Selected /
/// unselected styling matches the chips (`quickRow`): the everyday accent, or the warm explicit colours when
/// `explicit` is set. An option with no drawing (one added to the catalogue after the art was made) is the same
/// tile with its words alone, so the strip never shows a placeholder glyph.
struct PictographTile: View {
    let asset: String?
    let label: String
    let selected: Bool
    var explicit = false
    let action: () -> Void

    private var tint: Color { explicit ? Fx.explicitText : Fx.accentSoftText }
    private var fill: Color { explicit ? Fx.explicitFill : Fx.onTintStrong }
    private var stroke: Color { explicit ? Fx.explicitStroke : Fx.accent }
    private var idleStroke: Color { explicit ? Fx.explicitStroke.opacity(0.4) : Fx.hairline }

    var body: some View {
        Button(action: action) {
            VStack(spacing: 4) {
                if hasArt {
                    picto.frame(width: 44, height: 44)
                }
                Text(label)
                    .font(.system(size: 11, weight: selected ? .semibold : .regular))
                    .multilineTextAlignment(.center)
                    .lineLimit(hasArt ? 2 : 4)
                    .minimumScaleFactor(0.85)
                    .foregroundStyle(selected ? tint : Color.primary)
                    .frame(height: hasArt ? 28 : 76, alignment: hasArt ? .top : .center)
            }
            .frame(width: 76)
            .padding(.vertical, 8)
            .padding(.horizontal, 4)
            .background(RoundedRectangle(cornerRadius: Fx.tileRadius, style: .continuous).fill(selected ? fill : Fx.tile))
            .overlay(RoundedRectangle(cornerRadius: Fx.tileRadius, style: .continuous)
                .strokeBorder(selected ? stroke : idleStroke, lineWidth: 1))
            .contentShape(RoundedRectangle(cornerRadius: Fx.tileRadius, style: .continuous))
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
        .accessibilityAddTraits(selected ? [.isButton, .isSelected] : .isButton)
        .accessibilityHint(explicit ? "Explicit" : "")
    }

    /// True when the named drawing is really in the asset catalogue.
    private var hasArt: Bool {
        guard let asset else { return false }
        return UIImage(named: asset) != nil
    }

    /// The drawing. The art is template (monochrome) images, so it takes the tile's tint when selected.
    private var picto: some View {
        Image(asset ?? "").resizable().scaledToFit()
            .foregroundStyle(selected ? tint : Color.primary)
            .accessibilityHidden(true)
    }
}

// MARK: - "See all" sheet (11-19)

/// Every option of one quick-pick group at once, wrapped instead of in one long strip: what's picked sits at the top,
/// a search narrows the rest, and the 18+ options (only while 18+ is on) have their own flame-marked block. Taps
/// switch picks in the words straight away (the same `tapQuick` as the strips); Done just closes.
struct QuickPickSheet: View {
    let title: String
    let everyday: [String]
    let adult: [String]
    let isOn: (String) -> Bool
    let toggle: (String) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var search = ""

    private func matches(_ o: String) -> Bool {
        let q = search.trimmingCharacters(in: .whitespaces)
        return q.isEmpty || o.localizedCaseInsensitiveContains(q)
    }

    var body: some View {
        let picked = (everyday + adult).filter(isOn).reduce(into: [String]()) { if !$0.contains($1) { $0.append($1) } }
        let adultSet = Set(adult)
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    block(picked.isEmpty ? "Nothing picked yet" : "Picked (\(picked.count))",
                          picked, adultSet: adultSet, empty: "Tap any option below.")
                    block("Options", everyday.filter(matches), adultSet: [], empty: "No match.")
                    if !adult.isEmpty {
                        block("18+", adult.filter(matches), adultSet: adultSet, empty: "No match.", flame: true)
                    }
                }
                .padding(16)
            }
            .background(Fx.canvas.ignoresSafeArea())
            .searchable(text: $search, placement: .navigationBarDrawer(displayMode: .always), prompt: "Search \(title)")
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
        }
        .presentationDetents([.medium, .large])
        .presentationDragIndicator(.visible)
    }

    private func block(_ head: String, _ options: [String], adultSet: Set<String>, empty: String,
                       flame: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 4) {
                if flame { Image(systemName: "flame.fill").foregroundStyle(Fx.explicitText) }
                Text(head).font(.footnote.weight(.semibold)).foregroundStyle(Fx.secondaryText)
            }
            if options.isEmpty {
                Text(empty).font(.footnote).foregroundStyle(Fx.secondaryText)
            } else {
                FlowRows(spacing: 6) {
                    ForEach(options, id: \.self) { o in chip(o, explicit: adultSet.contains(o)) }
                }
            }
        }
    }

    private func chip(_ o: String, explicit: Bool) -> some View {
        let on = isOn(o)
        return Button { toggle(o) } label: {
            Text(o)
                .font(.system(size: 14, weight: on ? .semibold : .regular))
                .lineLimit(1)
                .foregroundStyle(on ? (explicit ? Fx.explicitText : Fx.accentSoftText) : Color.primary)
                .padding(.horizontal, 11)
                .frame(height: 32)
                .background(Capsule().fill(on ? (explicit ? Fx.explicitFill : Fx.onTintStrong) : Fx.tile))
                .overlay(Capsule().strokeBorder(on ? (explicit ? Fx.explicitStroke : Fx.accent)
                                                   : (explicit ? Fx.explicitStroke.opacity(0.4) : Color.clear),
                                                lineWidth: 1))
                .frame(minHeight: 44)
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(on ? .isSelected : [])
        .accessibilityHint(explicit ? "Explicit" : "")
    }
}

/// Lays chips out left to right, wrapping onto new lines (a plain flow layout).
struct FlowRows: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, line: CGFloat = 0, widest: CGFloat = 0
        for s in subviews {
            let size = s.sizeThatFits(.unspecified)
            if x > 0, x + size.width > width { y += line + spacing; x = 0; line = 0 }
            x += size.width + spacing
            line = max(line, size.height)
            widest = max(widest, x - spacing)
        }
        return CGSize(width: proposal.width ?? widest, height: y + line)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, line: CGFloat = 0
        for s in subviews {
            let size = s.sizeThatFits(.unspecified)
            if x > bounds.minX, x + size.width > bounds.maxX { y += line + spacing; x = bounds.minX; line = 0 }
            s.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            line = max(line, size.height)
        }
    }
}
