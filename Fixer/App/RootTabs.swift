import SwiftUI

/// The tabs at the bottom of the app. (Never name a type `Tab`: iOS 18 has one.)
enum RootTab: Hashable { case chat, make, library, settings }

/// Lets one screen send the owner to another tab, and tells the chat when "Clear everything" has run.
@MainActor
final class AppRouter: ObservableObject {
    @Published var tab: RootTab = .chat
    /// Bumped when Clear everything finishes, so the chat drops its draft, attachment and open viewers.
    @Published var clearedCount = 0
    /// Set by the Library or the chat to reuse an item's words, model and seed in Make.
    @Published var makeAgain: LibraryMeta?
}

/// Chat, Make, Library and Settings, each with its own navigation stack. The chat is created here so every tab shares it.
struct RootTabs: View {
    @StateObject private var chat = Chat()
    @StateObject private var router = AppRouter()
    @StateObject private var keyboard = KeyboardObserver()

    var body: some View {
        TabView(selection: $router.tab) {
            ChatScreen(chat: chat)
                .tabItem { Label("Chat", systemImage: "bubble.left.and.bubble.right") }
                .toolbar(.hidden, for: .tabBar)
                .tag(RootTab.chat)
            CreateScreen(chat: chat)
                .tabItem { Label("Make", systemImage: "paintbrush.pointed") }
                .toolbar(.hidden, for: .tabBar)
                .tag(RootTab.make)
            LibraryScreen(chat: chat)
                .tabItem { Label("Library", systemImage: "photo.stack") }
                .toolbar(.hidden, for: .tabBar)
                .tag(RootTab.library)
            SettingsScreen(chat: chat)
                .tabItem { Label("Settings", systemImage: "gearshape") }
                .toolbar(.hidden, for: .tabBar)
                .tag(RootTab.settings)
        }
        // One floating bar for every screen, drawn over the tabs. Each screen reserves the room itself with
        // `tabBarClearance()` inside its own navigation stack (an inset on the TabView does not reach the tabs), and the
        // bar steps aside while the keyboard is up.
        .overlay(alignment: .bottom) {
            if !keyboard.visible {
                FloatingTabBar(tab: $router.tab)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .fxAnimation(Motion.bar, value: keyboard.visible)
        .environmentObject(router)
        .environmentObject(keyboard)
    }
}
