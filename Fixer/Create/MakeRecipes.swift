import Foundation
import SwiftUI

// MARK: - What the computer sends for Make's recipe row (op `make_recipes`)

/// One pre-baked style: which model, shape and settings to use and the words that open and close the prompt.
/// (Not the same thing as `CreateRecipe`, which is a model the computer can run.)
struct StyleRecipe: Identifiable, Equatable {
    let id: String
    let name: String
    let group: String
    let kind: String                    // "picture" or "video"
    let explicit: Bool
    let model: String                   // a CreateRecipe id, or "video"
    let shape: String?                  // a shape name the computer already mapped
    let numbers: [String: Double]       // steps, cfg, shift
    let menus: [String: String]         // sampler, scheduler, explicit (labelled exactly as the model's menus are)
    let negative: String?
    let prefix: String                  // the prompt's opening words (any age comes from the owner's own words)
    let suffix: String
    let chips: [String]                 // which quick-pick groups to show
    let note: String?
    let explicitVideo: String?          // "on" / "off" for video recipes

    init?(_ d: [String: Any]) {
        guard let id = d["id"] as? String, !id.isEmpty else { return nil }
        self.id = id
        name = d["name"] as? String ?? id
        group = d["group"] as? String ?? ""
        kind = (d["kind"] as? String) == "video" ? "video" : "picture"
        explicit = d["explicit"] as? Bool ?? false
        model = d["model"] as? String ?? ""
        shape = d["shape"] as? String
        var nums: [String: Double] = [:]
        for (k, v) in (d["numbers"] as? [String: Any] ?? [:]) {
            if let n = (v as? NSNumber)?.doubleValue { nums[k] = n }
        }
        numbers = nums
        var ms: [String: String] = [:]
        for (k, v) in (d["menus"] as? [String: Any] ?? [:]) {
            if let s = v as? String { ms[k] = s }
        }
        menus = ms
        negative = d["negative"] as? String
        prefix = d["prefix"] as? String ?? ""
        suffix = d["suffix"] as? String ?? ""
        chips = d["chips"] as? [String] ?? []
        note = d["note"] as? String
        explicitVideo = (d["explicit_video"] as? String) ?? (d["explicitVideo"] as? String)
    }
}

/// One row of quick picks (Who, Body, Hair, ...). `explicitOptions` only show while the Explicit switch is on.
struct QuickGroup: Identifiable, Equatable {
    let id: String
    let label: String
    let options: [String]
    let explicitOptions: [String]

    init?(_ d: [String: Any]) {
        guard let id = d["id"] as? String, !id.isEmpty else { return nil }
        self.id = id
        label = d["label"] as? String ?? id.capitalized
        options = d["options"] as? [String] ?? []
        explicitOptions = (d["explicit_options"] as? [String]) ?? (d["explicitOptions"] as? [String]) ?? []
    }
}


struct MakeCatalog: Equatable {
    let recipes: [StyleRecipe]
    let groups: [QuickGroup]


    init(_ f: [String: Any]) {
        recipes = (f["recipes"] as? [[String: Any]] ?? []).compactMap(StyleRecipe.init)
        let qs = f["quickselect"] as? [String: Any] ?? [:]
        groups = (qs["groups"] as? [[String: Any]] ?? []).compactMap(QuickGroup.init)
  }
}

/// The last copy of the recipes, in memory only. Fetched every time Make opens, so the computer's edits show at once.
@MainActor
final class MakeRecipeStore: ObservableObject {
    static let shared = MakeRecipeStore()
    @Published private(set) var catalog: MakeCatalog?
    private var loading = false

    func refresh() async {
        guard Desktop.shared.isPaired, !loading else { return }
        loading = true
        defer { loading = false }
        do {
            catalog = try await Desktop.shared.makeRecipes()
        } catch {
            if Desktop.isOldComputer(error) { catalog = nil }
        }
    }

    /// Clear everything.
    func clear() { catalog = nil }
}

// MARK: - Form state that belongs to the recipes

/// Whether the dock edits a new picture or changes the picture on the canvas.
enum MakeTarget: Hashable { case new, change }

/// Which text field has the keyboard.
enum MakeField: Hashable { case prefix, words, change }

/// What a recipe set, so the row can say "Custom" once something was changed by hand.
struct StyleSnapshot: Equatable {
    var recipeID = ""
    var shape = ""
    var numbers: [String: Double] = [:]
    var menus: [String: String] = [:]
    var negative = ""
    var prefix = ""
    var suffix = ""
    var explicitVideo = ""
}

struct AppliedStyle: Equatable {
    let id: String
    let kind: String
    let numberKeys: [String]
    let menuKeys: [String]
    var snapshot: StyleSnapshot
}

// MARK: - Words in a prompt field: quick picks add and remove comma-separated phrases

enum PhraseList {
    private static func tokens(_ text: String) -> [String] {
        text.split(separator: ",", omittingEmptySubsequences: true)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }

    /// Whether `phrase` is one of the comma-separated phrases of `text` (any capitals).
    static func contains(_ text: String, _ phrase: String) -> Bool {
        let p = phrase.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return tokens(text).contains { $0.lowercased() == p }
    }

    /// Adds the phrase at the end, or takes it out (with its comma) when it is already there.
    static func toggled(_ text: String, _ phrase: String) -> String {
        let p = phrase.trimmingCharacters(in: .whitespacesAndNewlines)
        var list = tokens(text)
        if let i = list.firstIndex(where: { $0.lowercased() == p.lowercased() }) {
            list.remove(at: i)
        } else {
            list.append(p)
        }
        return list.joined(separator: ", ")
    }
}

