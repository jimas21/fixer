import SwiftUI

/// The "Auto ⌄" capsule in the composer's bottom row: which picture model the chat uses. "Auto" picks by the words.
/// Tapping it opens the cards. On the iPhone it only shows the phone model.
struct ModelChip: View {
    @ObservedObject private var rules = Rules.shared
    @ObservedObject private var desktop = Desktop.shared
    @State private var showCards = false
    @State private var recipes: [CreateRecipe] = []

    private var title: String {
        guard desktop.isOn else {
            let slot = ModelSlot.phoneImage
            return slot.builtIn.first { $0.id == slot.current }?.name ?? ModelEntry.displayName(slot.current)
        }
        if rules.chatModelChoice == "auto" { return "Auto" }
        // 11-19: an 18+ model chosen earlier isn't used while 18+ is off (the computer picks the everyday one).
        guard let r = recipes.first(where: { $0.id == rules.chatModelChoice }), rules.explicitNow || !r.adult
        else { return "Auto" }
        return r.name
    }

    var body: some View {
        Button { showCards = true } label: {
            HugUpTo(limit: 170) {
                HStack(spacing: 3) {
                    Text(title).font(.subheadline).lineLimit(1).truncationMode(.tail)
                    Image(systemName: "chevron.down").font(.system(size: 11, weight: .bold)).foregroundStyle(Fx.secondaryText)
                }
                .foregroundStyle(.primary)
                .padding(.horizontal, 12)
                .frame(minHeight: 36)
                .background(Capsule().fill(Fx.tile))
            }
            .padding(.horizontal, 4)                    // with the round buttons' own 4 pt, an even 8 pt between all
            .frame(minHeight: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Picture model: \(title)")
        .accessibilityHint("Opens the list of picture models")
        .sheet(isPresented: $showCards) { ModelCardSheet() }
        .task(id: desktop.isOn) {
            guard desktop.isOn else { return }
            recipes = await desktop.imageRecipes()
        }
    }
}

/// Exactly as wide as its content, up to `limit`. (`.frame(maxWidth:)` would take the whole limit and float the
/// capsule in the middle of it, pushing the composer's buttons apart.)
private struct HugUpTo: Layout {
    var limit: CGFloat

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        guard let content = subviews.first else { return .zero }
        return content.sizeThatFits(ProposedViewSize(width: Swift.min(proposal.width ?? limit, limit), height: proposal.height))
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        subviews.first?.place(at: CGPoint(x: bounds.minX, y: bounds.midY), anchor: .leading,
                              proposal: ProposedViewSize(width: bounds.width, height: bounds.height))
    }
}

/// One choice in the sheet.
struct ModelCard: Identifiable {
    let id: String
    let name: String
    let note: String
}

struct ModelCardSheet: View {
    @ObservedObject private var rules = Rules.shared
    @ObservedObject private var desktop = Desktop.shared
    @Environment(\.dismiss) private var dismiss
    @State private var recipes: [CreateRecipe] = []
    @State private var loaded = false

    private var cards: [ModelCard] {
        let on = rules.explicitNow
        let auto = ModelCard(id: "auto", name: "Auto", note: on
                             ? "Uses the 18+ models, picked by your words: new picture or edit."
                             : "Uses the everyday model. 18+ is off for this session.")
        let shown: [CreateRecipe] = recipes.filter { on || !$0.adult }     // 11-19: 18+ models only while 18+ is on
        let rest: [ModelCard] = shown.map {
            ModelCard(id: $0.id, name: $0.name, note: $0.note ?? "Used for every picture you ask for in the chat.")
        }
        return [auto] + rest
    }

    var body: some View {
        NavigationStack {
            List {
                if desktop.isOn {
                    Section {
                        ForEach(cards) { card in
                            Button {
                                rules.chatModelChoice = card.id
                                dismiss()
                            } label: {
                                HStack(alignment: .top) {
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(card.name).foregroundStyle(.primary)
                                        Text(card.note).font(.footnote).foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                    if rules.chatModelChoice == card.id {
                                        Image(systemName: "checkmark").foregroundStyle(Color.accentColor)
                                    }
                                }
                                .frame(minHeight: 44)
                                .contentShape(Rectangle())
                            }
                            .accessibilityAddTraits(rules.chatModelChoice == card.id ? .isSelected : [])
                        }
                        if !loaded {
                            HStack { ProgressView(); Text("Asking your computer…").foregroundStyle(.secondary) }
                        }
                    } header: {
                        Text("Pictures")
                    } footer: {
                        Text("Your choice applies to pictures made in the chat. Make has its own model list.")
                    }
                    Section {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Wan 2.2 14B")
                            Text(rules.explicitNow ? "The explicit add-on is decided by your words."
                                 : "No explicit add-on: 18+ is off for this session.")
                                .font(.footnote).foregroundStyle(.secondary)
                        }
                    } header: {
                        Text("Video")
                    } footer: {
                        Text("For information: videos always use this model.")
                    }
                } else {
                    Section {
                        let slot = ModelSlot.phoneImage
                        LabeledContent("Picture model",
                                       value: slot.builtIn.first { $0.id == slot.current }?.name ?? ModelEntry.displayName(slot.current))
                    } footer: {
                        Text("Change it in Settings › Advanced.")
                    }
                }
            }
            .navigationTitle("Picture model")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() } }
            }
            .task {
                guard desktop.isOn else { loaded = true; return }
                recipes = await desktop.imageRecipes()
                loaded = true
            }
        }
        .presentationDetents([.medium, .large])
    }
}
