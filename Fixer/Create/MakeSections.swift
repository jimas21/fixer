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
    /// exists, plain chips otherwise). Callers (`composeStack`) still reach it as `composeQuickCard` in MakeCompose.
    var quickSectionsCard: some View {
        MakeSectionsCard(
            sections: quickSections,
            selectedCount: { quickSelectedCount($0) },
            row: { AnyView(quickRowTiled($0)) },
            expanded: $quickExpanded
        )
    }

    // MARK: tiled rows (Goal 2 plumbing)

    /// Like `quickRow`, but renders the group's options as pictograph tiles IF any visible option has art; otherwise
    /// it is exactly the plain chip row. Taps go through the same `tapQuick`, so selection logic is shared.
    @ViewBuilder
    func quickRowTiled(_ g: QuickGroup) -> some View {
        let options = g.options + (makeExplicit ? g.explicitOptions : [])
        if options.contains(where: { Pictograph.asset(groupID: g.id, option: $0) != nil }) {
            tiledGroup(g, options: options)
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
            Text(g.label).font(.footnote).foregroundStyle(Fx.secondaryText).lineLimit(1)
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
    let selectedCount: (QuickGroup) -> Int
    let row: (QuickGroup) -> AnyView
    /// Collapsed by default: a section name is in here only after it has been opened.
    @Binding var expanded: Set<String>

    var body: some View {
        VStack(spacing: 10) {
            ForEach(sections) { section in
                sectionCard(section)
            }
        }
    }

    private func sectionCard(_ section: QuickSection) -> some View {
        let isOpen = expanded.contains(section.name)
        let count = section.groups.reduce(0) { $0 + selectedCount($1) }
        return VStack(alignment: .leading, spacing: 8) {
            Button {
                if expanded.contains(section.name) { expanded.remove(section.name) }
                else { expanded.insert(section.name) }
            } label: {
                header(name: section.name, count: count, isOpen: isOpen)
            }
            .buttonStyle(.plain)
            .accessibilityLabel(count > 0 ? "\(section.name), \(count) selected" : section.name)
            .accessibilityHint(isOpen ? "Collapse" : "Expand")
            if isOpen {
                VStack(spacing: 0) { ForEach(section.groups) { g in row(g) } }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Fx.tile))
        .fxAnimation(Motion.pop, value: isOpen)
    }

    private func header(name: String, count: Int, isOpen: Bool) -> some View {
        HStack(spacing: 8) {
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
