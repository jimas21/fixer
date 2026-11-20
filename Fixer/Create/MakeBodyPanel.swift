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
    /// 11-19: hidden by default: every slider term is also a chip in the Body section (Build, Figure, Fitness,
    /// Height), whose add sheet now covers them; the owner found the two duplicating each other. phone_config.json
    /// "make_body_sliders": "on" brings the panel back without an app update.
    var showBodyPanel: Bool {
        guard RemoteConfig.string("make_body_sliders") == "on" else { return false }
        let bodyIDs: Set<String> = ["body", "body_size", "fitness", "height"]
        return activeGroups.contains { bodyIDs.contains($0.id) }
    }

    /// Position for an axis: 0 = none, 1...n = the active term. Read back from the prompt each render, so the slider
    /// reflects a term set by a chip too.
    func bodyAxisValue(_ axis: BodyAxis) -> Int {
        let text = quickBinding.wrappedValue
        for (i, t) in axis.terms.enumerated() where PhraseList.contains(text, t) { return i + 1 }
        return 0
    }

    /// Writes one axis position into the prompt words (dropping the axis's other terms first), with one haptic tick.
    func commitBodyAxis(_ axis: BodyAxis, _ idx: Int) {
        let add = (1...axis.terms.count).contains(idx) ? axis.terms[idx - 1] : nil
        let b = quickBinding
        b.wrappedValue = PhraseList.replacing(b.wrappedValue, remove: axis.terms, with: add)
        quickTick += 1
    }

    var bodyPanel: some View {
        VStack(spacing: 16) {
            ForEach(Self.bodyAxes) { axis in
                BodyAxisSlider(label: axis.label, terms: axis.terms, value: bodyAxisValue(axis)) { idx in
                    commitBodyAxis(axis, idx)
                }
            }
        }
    }
}

/// One graded Body slider. While a finger is on it, the position lives only here: the label previews the term live,
/// but the prompt is not touched and nothing buzzes. On release the chosen stop is written ONCE, with one soft tick.
/// (It used to write the prompt and fire a haptic on every step crossed mid-drag, which buzzed constantly and
/// re-rendered the whole Make sheet per step.) A change made without a drag (VoiceOver adjust) commits at once.
struct BodyAxisSlider: View {
    let label: String
    let terms: [String]
    let value: Int                      // committed position from the prompt: 0 = none, 1...terms.count
    let commit: (Int) -> Void
    @State private var live: Double?    // position during a drag; nil when not dragging
    @State private var editing = false

    private var shown: Int { Int((live ?? Double(value)).rounded()) }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(label).font(.footnote.weight(.semibold))
                Spacer()
                Text(shown == 0 ? "—" : terms[shown - 1])
                    .font(.footnote)
                    .foregroundStyle(shown == 0 ? Fx.secondaryText : Fx.accentSoftText)
            }
            Slider(
                value: Binding(
                    get: { live ?? Double(value) },
                    set: { v in
                        if editing { live = v }
                        else if Int(v.rounded()) != value { commit(Int(v.rounded())) }
                    }
                ),
                in: 0...Double(terms.count), step: 1,
                onEditingChanged: { began in
                    editing = began
                    guard !began else { return }
                    let s = shown
                    live = nil
                    if s != value { commit(s) }
                }
            )
            .tint(Fx.accent)
            .accessibilityLabel(label)
            .accessibilityValue(shown == 0 ? "not set" : terms[shown - 1])
        }
    }
}
