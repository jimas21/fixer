import SwiftUI

/// The floating tab bar from the mock-up: Chat, Make, Library, Settings on every screen.
struct FloatingTabBar: View {
    @Binding var tab: RootTab

    private struct Item: Identifiable {
        let tab: RootTab
        let title: String
        let icon: String
        var id: String { title }
    }

    private let items: [Item] = [
        Item(tab: .chat, title: "Chat", icon: "bubble.left"),
        Item(tab: .make, title: "Make", icon: "wand.and.stars"),
        Item(tab: .library, title: "Library", icon: "photo.stack"),
        Item(tab: .settings, title: "Settings", icon: "slider.horizontal.3"),
    ]

    var body: some View {
        HStack(spacing: 0) {
            ForEach(items) { item in
                Button {
                    tab = item.tab
                } label: {
                    VStack(spacing: 2) {
                        Image(systemName: item.icon).font(.system(size: 22))
                        Text(item.title).font(.caption2.weight(.semibold)).lineLimit(1)
                    }
                    .foregroundStyle(tab == item.tab ? Fx.accentText : Fx.tabIdle)
                    .frame(maxWidth: .infinity, minHeight: 52)
                    .background {
                        if tab == item.tab {
                            Capsule().fill(Color.primary.opacity(0.10))
                        }
                    }
                    .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(item.title)
                .accessibilityAddTraits(tab == item.tab ? [.isButton, .isSelected] : .isButton)
                .accessibilityShowsLargeContentViewer {
                    Label(item.title, systemImage: item.icon)
                }
            }
        }
        .padding(5)
        .dynamicTypeSize(...DynamicTypeSize.xLarge)
        .glass(radius: Fx.tabBarRadius, tint: Fx.bar)
        .padding(.horizontal, 16)
        .padding(.bottom, 2)
        .sensoryFeedback(.selection, trigger: tab)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Tabs")
    }
}

extension FloatingTabBar {
    /// The room the bar takes above the home indicator: 52 pt items, 5 pt padding either side, 2 pt below.
    static let clearance: CGFloat = 52 + 10 + 2
}

/// Keeps a screen's content (and its own bottom controls) clear of the floating tab bar. Put it on the screen's root
/// view inside its navigation stack; it gives the room back while the keyboard is up (the bar is hidden then).
private struct TabBarClearance: ViewModifier {
    @EnvironmentObject private var keyboard: KeyboardObserver

    func body(content: Content) -> some View {
        content.safeAreaInset(edge: .bottom, spacing: 0) {
            Color.clear.frame(height: keyboard.visible ? 0 : FloatingTabBar.clearance)
        }
    }
}

extension View {
    func tabBarClearance() -> some View { modifier(TabBarClearance()) }
}
