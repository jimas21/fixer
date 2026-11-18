import Foundation
import SwiftUI
import UIKit

/// Nothing Fixer keeps goes into iCloud or computer backups (the owner 11-17: "we shouldn't be iCloud backing up any of this
/// tool on the phone"). Excluding a folder excludes everything in it, now and later, so the app's whole Application
/// Support and Documents folders (conversations, photos, videos, Create pictures, on-phone models) and its settings
/// file are marked at every launch. Caches and tmp are never backed up by iOS. The PC link lives in the keychain as
/// "this device only", which no backup carries either.
enum Privacy {
    /// A fresh install (no settings yet) must not inherit a PC pairing key from an earlier install: the keychain
    /// outlives app deletion.
    static func forgetOldInstallKey() {
        let d = UserDefaults.standard
        guard d.object(forKey: "app.installed") == nil else { return }
        if !d.dictionaryRepresentation().keys.contains(where: { $0.hasPrefix("rules.") }) {
            PCLink.forget()
            LibraryKeys.forget()                    // the Library's keys outlive a deleted app too
        }
        d.set(true, forKey: "app.installed")
    }

    /// Covers every window (sheets and full-screen screens too) whenever Fixer isn't in front, so the app-switcher
    /// snapshot iOS keeps shows nothing of the conversation.
    @MainActor private static var cover: UIWindow?

    @MainActor static func coverScreen(_ on: Bool) {
        if !on { cover?.isHidden = true; cover = nil; return }
        guard cover == nil, let scene = UIApplication.shared.connectedScenes
            .compactMap({ $0 as? UIWindowScene }).first else { return }
        let w = UIWindow(windowScene: scene)
        w.windowLevel = .alert + 1
        let vc = UIHostingController(rootView: ZStack {
            Color(.systemBackground).ignoresSafeArea()
            Image(systemName: "lock.fill").font(.largeTitle).foregroundStyle(.secondary)
        })
        w.rootViewController = vc
        w.isHidden = false
        cover = w
    }

    static func excludeFromBackup() {
        let fm = FileManager.default
        var places: [URL] = []
        for dir in [FileManager.SearchPathDirectory.applicationSupportDirectory, .documentDirectory] {
            if let url = fm.urls(for: dir, in: .userDomainMask).first {
                try? fm.createDirectory(at: url, withIntermediateDirectories: true)
                places.append(url)
            }
        }
        if let library = fm.urls(for: .libraryDirectory, in: .userDomainMask).first,
           let bundle = Bundle.main.bundleIdentifier {
            places.append(library.appendingPathComponent("Preferences/\(bundle).plist"))
        }
        for var url in places where fm.fileExists(atPath: url.path) {
            var values = URLResourceValues()
            values.isExcludedFromBackup = true
            try? url.setResourceValues(values)
        }
    }
}
