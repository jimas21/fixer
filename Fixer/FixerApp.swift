import SwiftUI

@main
struct FixerApp: App {
    @Environment(\.scenePhase) private var phase

    @State private var pendingLink: PCLink?

    init() {
        Privacy.forgetOldInstallKey()
        Privacy.excludeFromBackup()
        LibraryVault.sweepOrphans()               // half-saved Library files nobody can read; before any save starts
    }

    var body: some Scene {
        WindowGroup {
            RootTabs()
                .onOpenURL { url in
                    // fixer://pair?... from the QR code pair.py shows on the PC. Asked first: a link from anywhere
                    // else must never quietly re-pair this phone.
                    if let link = PCLink.from(url: url) { pendingLink = link }
                }
                .alert("Connect to a computer?", isPresented: Binding(get: { pendingLink != nil },
                                                                     set: { if !$0 { pendingLink = nil } })) {
                    Button("Connect") { if let l = pendingLink { Desktop.shared.pair(l) }; pendingLink = nil }
                    Button("Cancel", role: .cancel) { pendingLink = nil }
                } message: {
                    Text("This replaces the current connection. Address: "
                         + [pendingLink?.home, pendingLink?.away].compactMap { $0 }.joined(separator: " and ")
                         + ". Only connect with a code from your own computer.")
                }
        }
        .onChange(of: phase) { _, now in
            // Opening Fixer wakes the computer: the chat model loads now, so the first message doesn't wait.
            // 11-17: and finished work it kept for this iPhone while Fixer was closed is collected where it was asked for.
            if now == .active {
                Desktop.shared.warmUp()
                Task { await JobInbox.shared.sweep() }
            }
            Privacy.coverScreen(now != .active)        // the app-switcher snapshot shows nothing
            // Settings are rewritten as a new file (which drops the mark), so it's set again on leaving the app.
            if now == .background {
                Privacy.excludeFromBackup()
                LibraryStore.shared.lock()               // the Library opens again only with Face ID or the passcode
            }
        }
    }
}
