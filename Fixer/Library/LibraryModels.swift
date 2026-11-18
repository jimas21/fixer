import Foundation

enum LibraryKind: String, Codable { case picture, video }

/// Where an item came from: made in the chat, made in Make, or a photo the owner added.
enum LibrarySource: String, Codable { case chat, make, added }

struct LoraSetting: Codable, Equatable {
    var name: String
    var strength: Double
}

/// What is known about one Library item. It is stored encrypted next to the item. Everything beyond the first four
/// fields is Optional, so items saved by an earlier build still open after fields are added.
struct LibraryMeta: Codable, Equatable {
    var id: UUID
    var kind: LibraryKind
    var source: LibrarySource
    var created: Date
    var action: String?            // make, edit, remove, cutout, upscale, video, animate, added
    var words: String?
    var model: String?
    var recipeID: String?
    var choice: String?            // "auto" or a recipe id
    var seed: Int?
    var texts: [String: String]?
    var numbers: [String: Double]?
    var menus: [String: String]?
    var shape: String?
    var toggles: [String]?
    var loras: [LoraSetting]?
    var explicitVideo: String?
    var enhanced: String?          // the prompt the computer wrote from the owner's words, when Enhance was on
    var imageMode: String?
    var strength: Double?
    var conversation: UUID?
    var parent: UUID?
    var width: Int?
    var height: Int?
    var thumbnail: Data?

    init(kind: LibraryKind, source: LibrarySource, action: String? = nil) {
        id = UUID()
        self.kind = kind
        self.source = source
        created = Date()
        self.action = action
    }
}

/// What the chat knows about a result it is adding to the Library.
struct Made {
    var source: LibrarySource = .chat
    var action: String
    var words: String?
    var model: String?
    var recipeID: String?
    var choice: String?
    var seed: Int?
    var explicitVideo: String?
    var enhanced: String?
    var parent: UUID?

    func meta(kind: LibraryKind, conversation: UUID?) -> LibraryMeta {
        var m = LibraryMeta(kind: kind, source: source, action: action)
        m.words = words
        m.model = model
        m.recipeID = recipeID
        m.choice = choice
        m.seed = seed
        m.explicitVideo = explicitVideo
        m.enhanced = enhanced
        m.parent = parent
        m.conversation = conversation
        return m
    }
}

/// How much is in the Library, read from file names alone (no key, no unlock).
struct LibraryInventory: Equatable {
    var pictures = 0
    var videos = 0
    var protected = 0
    var bytes: Int64 = 0
    var items: Int { pictures + videos }
    var unprotected: Int { items - protected }
}

/// One item as the Library screen shows it (only exists while the Library is open).
struct LibraryEntry: Identifiable {
    let id: UUID
    var meta: LibraryMeta
    var isProtected: Bool
}

enum LibraryError: LocalizedError {
    case keys, damaged
    var errorDescription: String? {
        switch self {
        case .keys: "The Library's key isn't available."
        case .damaged: "That item couldn't be read."
        }
    }
}
