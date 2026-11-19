import SwiftUI

/// The Body panel: graded sliders for the figure, a precision alternative to the Body quick-pick chips. Each axis owns
/// an ordered set of mutually-exclusive terms; the slider sets exactly one (or none at the low end) in the SAME prompt
/// words the chips edit, so the two stay in sync and nothing special is needed on send. The server's per-model grammar
/// then keeps or strips any weighting per engine. Shown only for person recipes (those with a body quick-pick group).
extension CreateScreen {

    /// One graded axis. `terms` run low → high; slider stop 0 means "leave it to the prompt" (no term set).
    struct BodyAxis: Identifiable {
        let id: String
        let label: String
        let terms: [String]
    }

    static let bodyAxes: [BodyAxis] = [
        .init(id: "build",   label: "Build",   terms: ["very slim", "slim", "average build", "thick", "plus-size"]),
        .init(id: "fitness", label: "Fitness", terms: ["soft", "average fitness", "toned", "athletic", "very lean", "ripped"]),
        .init(id: "bust",    label: "Bust",    terms: ["small bust", "modest bust", "medium bust", "full bust", "large bust", "ample bust"]),
        .init(id: "curves",  label: "Curves",  terms: ["slim figure", "softly curvy", "curvy", "very curvy"]),
        .init(id: "height",  label: "Height",  terms: ["short", "average height", "tall", "very tall"]),
    ]

    /// Worth showing only when the recipe is about a person (its quick picks include a body group).
    var showBodyPanel: Bool {
        let bodyIDs: Set<String> = ["body", "body_size", "fitness", "height"]
        return activeGroups.contains { bodyIDs.contains($0.id) }
    }

    /// Slider position for an axis: 0 = none, 1...n = the active term. Read back from the prompt each render, so the
    /// slider reflects a term set by a chip too, and writing removes the axis's other terms before adding the new one.
    func bodyAxisBinding(_ axis: BodyAxis) -> Binding<Double> {
        Binding(
            get: {
                let text = self.quickBinding.wrappedValue
                for (i, t) in axis.terms.enumerated() where PhraseList.contains(text, t) {
                    return Double(i + 1)
                }
                return 0
            },
            set: { newValue in
                let idx = Int(newValue.rounded())
                let add = (1...axis.terms.count).contains(idx) ? axis.terms[idx - 1] : nil
                let b = self.quickBinding
                b.wrappedValue = PhraseList.replacing(b.wrappedValue, remove: axis.terms, with: add)
                self.quickTick += 1
            }
        )
    }

    var bodyPanel: some View {
        VStack(spacing: 16) {
            ForEach(Self.bodyAxes) { axis in
                bodyAxisRow(axis)
            }
        }
    }

    @ViewBuilder
    private func bodyAxisRow(_ axis: BodyAxis) -> some View {
        let binding = bodyAxisBinding(axis)
        let idx = Int(binding.wrappedValue.rounded())
        let current = (1...axis.terms.count).contains(idx) ? axis.terms[idx - 1] : "—"
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(axis.label).font(.footnote.weight(.semibold))
                Spacer()
                Text(current)
                    .font(.footnote)
                    .foregroundStyle(idx == 0 ? Fx.secondaryText : Fx.accentSoftText)
            }
            Slider(value: binding, in: 0...Double(axis.terms.count), step: 1)
                .tint(Fx.accent)
        }
    }
}
