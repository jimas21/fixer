import Security
import SwiftUI

/// 11-20 (the owner: "a convenient way to delete both ends when we don't want it anymore"). "Remove Fixer everywhere":
/// the computer uninstalls Fixer Desktop completely (nothing kept), then this iPhone wipes everything Fixer holds:
/// chats, pictures, videos, songs, the Library, downloaded models, settings, the pairing and its keys. iOS lets no
/// app delete itself, so it ends by saying how to remove the icon (TestFlight puts it back).
enum AppWipe {
    /// Everything in the app's own folders, its keychain items and its settings.
    static func everything() {
        SealedFiles.forgetAll()                    // every key held in memory, and the decrypted temporary copies
        let jar = HTTPCookieStorage.shared
        for cookie in jar.cookies ?? [] { jar.deleteCookie(cookie) }
        let fm = FileManager.default
        for library in fm.urls(for: .libraryDirectory, in: .userDomainMask) {
            for name in ["Cookies", "HTTPStorages"] {
                try? fm.removeItem(at: library.appendingPathComponent(name, isDirectory: true))
            }
        }
        var dirs = [fm.temporaryDirectory]
        for d in [FileManager.SearchPathDirectory.documentDirectory, .applicationSupportDirectory, .cachesDirectory] {
            dirs += fm.urls(for: d, in: .userDomainMask)
        }
        for dir in dirs {
            for item in (try? fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)) ?? [] {
                try? fm.removeItem(at: item)
            }
        }
        for c in [kSecClassGenericPassword, kSecClassInternetPassword, kSecClassKey, kSecClassCertificate, kSecClassIdentity] {
            SecItemDelete([kSecClass as String: c] as CFDictionary)
        }
        if let id = Bundle.main.bundleIdentifier { UserDefaults.standard.removePersistentDomain(forName: id) }
        UserDefaults.standard.synchronize()
    }
}

struct RemoveEverywhereSection: View {
    @ObservedObject var chat: Chat
    @State private var asking = false
    @State private var running = false

    var body: some View {
        Section {
            Button(role: .destructive) { asking = true } label: {
                Label("Remove Fixer everywhere", systemImage: "xmark.bin").frame(maxWidth: .infinity, alignment: .leading)
            }
            .confirmationDialog("Remove Fixer from this iPhone and your computer?", isPresented: $asking,
                                titleVisibility: .visible) {
                Button("Remove everything", role: .destructive) { running = true }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Your computer uninstalls Fixer Desktop and deletes it all: models, styles, settings and the "
                     + "pairing. This iPhone deletes every chat, picture, video, song, Library item, model and "
                     + "setting. Nothing is kept. This can't be undone.")
            }
            .fullScreenCover(isPresented: $running) { RemoveEverywhereView(chat: chat) }
        } footer: {
            Text("For when you're done with Fixer: wipes and removes it on both ends. Then you remove the app's icon.")
        }
    }
}

struct RemoveEverywhereView: View {
    @ObservedObject var chat: Chat
    @State private var pcLine = "Asking your computer to remove Fixer Desktop…"
    @State private var pcDone = false
    @State private var phoneDone = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Spacer(minLength: 0)
            row(pcDone, pcLine)
            row(phoneDone, phoneDone ? "This iPhone: everything Fixer had is deleted." : "Wiping this iPhone…")
            if phoneDone {
                VStack(alignment: .leading, spacing: 8) {
                    Text("One last step").font(.title3.weight(.semibold))
                    Text("iPhone apps can't delete themselves. Go to your Home Screen, press and hold the Fixer icon, "
                         + "tap Remove App, then Delete App.")
                    Text("To use Fixer again later: install it from TestFlight, run the Fixer Desktop installer on "
                         + "your computer, and pair.")
                        .foregroundStyle(.secondary)
                }
                .padding(.top, 8)
            }
            Spacer(minLength: 0)
        }
        .padding(24)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Fx.canvas.ignoresSafeArea())
        .interactiveDismissDisabled()
        .task { await run() }
    }

    private func row(_ done: Bool, _ text: String) -> some View {
        HStack(alignment: .top, spacing: 12) {
            if done { Image(systemName: "checkmark.circle.fill").foregroundStyle(.green) } else { ProgressView() }
            Text(text)
        }
    }

    private func run() async {
        if Desktop.shared.isPaired {
            switch await Desktop.shared.removeFromComputer() {
            case .removing: pcLine = "Your computer is removing Fixer Desktop now. It takes about a minute."
            case .unreachable: pcLine = "Your computer couldn't be reached. To remove it there, run "
                + "C:\\FixerDesktop\\uninstall.ps1 (right-click, Run with PowerShell)."
            case .failed(let why): pcLine = "Your computer said: \(why) To remove it there, run C:\\FixerDesktop\\uninstall.ps1."
            }
        } else {
            pcLine = "No computer is paired."
        }
        pcDone = true
        SongPlayer.shared.stop()
        SongStore.shared.cancel()
        chat.wipeSession()
        CreateStore.shared.clear()
        History.deleteAll()
        Brain.shared.forget()
        Desktop.shared.unpair()
        await LibraryStore.shared.flush()          // a picture still being sealed would write its files and keys after the wipe
        AppWipe.everything()
        try? await Task.sleep(nanoseconds: 400_000_000)
        await LibraryStore.shared.flush()
        AppWipe.everything()                       // once more, in case a save finished meanwhile
        phoneDone = true
    }
}
