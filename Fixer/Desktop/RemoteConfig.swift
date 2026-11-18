import Foundation

/// Texts, word lists and defaults that Fixer Desktop can change without an app update (its phone_config.json):
/// the chat model's tool instructions, the default personality, the words that make a message a picture request,
/// the clothing check. Fetched when the app opens; the last copy is kept, so it applies when the computer is off.
/// Anything missing falls back to the text built into the app.
enum RemoteConfig {
    private static let key = "remote.config"

    nonisolated static func string(_ name: String) -> String? {
        guard let s = UserDefaults.standard.dictionary(forKey: key)?[name] as? String, !s.isEmpty else { return nil }
        return s
    }

    nonisolated static func list(_ name: String) -> [String]? {
        guard let l = UserDefaults.standard.dictionary(forKey: key)?[name] as? [String], !l.isEmpty else { return nil }
        return l
    }

    /// Keeps only strings and string lists (all UserDefaults can store safely).
    nonisolated static func save(_ config: [String: Any]) {
        var clean: [String: Any] = [:]
        for (k, v) in config {
            if let s = v as? String { clean[k] = s } else if let l = v as? [String] { clean[k] = l }
        }
        UserDefaults.standard.set(clean, forKey: key)
    }
}
