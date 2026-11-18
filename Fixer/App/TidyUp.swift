import SwiftUI

/// 11-18: "Free up my computer" and "Erase everything", the one place for their words and their two flows. Every
/// screen that offers them (Make, Chat, History, Queue, Library, Settings) uses what is here and keeps no copy.
enum TidyText {
    static let freeTitle = "Free up my computer"
    static let freeCaption = "Stops anything running and unloads every model from its memory. Nothing is erased. "
        + "The next picture or chat takes a little longer to start."
    static let freeConfirmTitle = "Free up your computer?"
    static let freeConfirmButton = "Free it up"
    static let freeDone = "Your computer is free."

    static let eraseTitle = "Erase everything"
    static let eraseCaption = "Deletes every chat, picture and video on this iPhone and on your computer. "
        + "Protected Library items stay. This can't be undone."
    static let eraseConfirmTitle = "Erase everything?"
    static let eraseConfirmButton = "Erase everything"

    static let oldComputer = "Your computer needs the newer Fixer Desktop for that."
}

/// The free flow's state: confirm, then Desktop.freeMemory(). A nil answer means the computer is too old for it.
@MainActor
final class TidyFree: ObservableObject {
    @Published var asking = false
    @Published var working = false
    @Published var message: String?

    func run(after: @escaping () -> Void = {}) {
        guard !working else { return }
        working = true
        message = nil
        Task {
            do {
                let answer = try await Desktop.shared.freeMemory()
                message = answer == nil ? TidyText.oldComputer : TidyText.freeDone
            } catch {
                message = error.localizedDescription
            }
            working = false
            after()
        }
    }
}

/// The free flow's confirmation, and (for the menu, which has no room for a line of text) the result as an alert.
private struct TidyFreeFlow: ViewModifier {
    @ObservedObject var model: TidyFree
    var alertResult: Bool
    var onDone: () -> Void

    func body(content: Content) -> some View {
        content
            .confirmationDialog(TidyText.freeConfirmTitle, isPresented: $model.asking, titleVisibility: .visible) {
                Button(TidyText.freeConfirmButton, role: .destructive) { model.run(after: onDone) }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text(TidyText.freeCaption)
            }
            .alert(TidyText.freeTitle, isPresented: Binding(
                get: { alertResult && model.message != nil },
                set: { if !$0 { model.message = nil } })) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(model.message ?? "")
            }
    }
}

/// The erase flow: confirm, then ClearEverythingView straight into erasing. `asking` is set by whoever shows the
/// button. It always opens in a sheet with its own navigation stack: the buttons sit in menus and list rows, where a
/// pushed screen is not reliable.
private struct TidyEraseFlow: ViewModifier {
    @ObservedObject var chat: Chat
    @Binding var asking: Bool
    var onClear: () -> Void
    var onFinished: () -> Void
    @State private var sheeted = false

    private func finish() {
        sheeted = false
        onFinished()
    }

    func body(content: Content) -> some View {
        content
            .confirmationDialog(TidyText.eraseConfirmTitle, isPresented: $asking, titleVisibility: .visible) {
                Button(TidyText.eraseConfirmButton, role: .destructive) { sheeted = true }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text(TidyText.eraseCaption)
            }
            .sheet(isPresented: $sheeted) {
                NavigationStack {
                    ClearEverythingView(chat: chat, onClear: onClear, onFinished: { finish() }, startNow: true)
                }
            }
    }
}

/// A Form / List section: the free button with its caption as the footer. Shown only when a computer is connected.
struct TidyFreeSection: View {
    @ObservedObject private var desktop = Desktop.shared
    @StateObject private var model = TidyFree()
    var onDone: () -> Void = {}

    var body: some View {
        if desktop.isOn {
            Section {
                Button { model.asking = true } label: {
                    Label(TidyText.freeTitle, systemImage: "memorychip").frame(maxWidth: .infinity, alignment: .leading)
                }
                .disabled(model.working)
                .modifier(TidyFreeFlow(model: model, alertResult: false, onDone: onDone))
                if let message = model.message {
                    Text(message).font(.footnote).foregroundStyle(.secondary)
                }
            } footer: {
                Text(TidyText.freeCaption)
            }
        }
    }
}

/// A Form / List section: the erase button with its caption as the footer.
struct TidyEraseSection: View {
    @ObservedObject var chat: Chat
    var onClear: () -> Void
    var onFinished: () -> Void = {}
    @State private var asking = false

    var body: some View {
        Section {
            Button(role: .destructive) { asking = true } label: {
                Label(TidyText.eraseTitle, systemImage: "trash").frame(maxWidth: .infinity, alignment: .leading)
            }
            .modifier(TidyEraseFlow(chat: chat, asking: $asking, onClear: onClear, onFinished: onFinished))
        } footer: {
            Text(TidyText.eraseCaption)
        }
    }
}

/// The round button the menu wears, like the queue button beside it in Make.
struct TidyCircleLabel: View {
    var large = false

    var body: some View {
        Image(systemName: "ellipsis")
            .font(.system(size: large ? 20 : 18, weight: large ? .regular : .semibold))
            .foregroundStyle(.primary)
            .frame(width: 44, height: 44)
            .background(Circle().fill(Fx.bar))
            .overlay(Circle().strokeBorder(Fx.hairline, lineWidth: 0.5))
            .contentShape(Circle())
    }
}

/// One compact button for toolbars and the Make header: a menu with the two actions (the captions show in the
/// confirmation). The erase screen opens in a sheet.
struct TidyMenu<Icon: View>: View {
    @ObservedObject var chat: Chat
    var onClear: () -> Void
    var label: () -> Icon
    @ObservedObject private var desktop = Desktop.shared
    @StateObject private var model = TidyFree()
    @State private var askingErase = false

    init(chat: Chat, onClear: @escaping () -> Void, @ViewBuilder label: @escaping () -> Icon) {
        self.chat = chat
        self.onClear = onClear
        self.label = label
    }

    var body: some View {
        Menu {
            if desktop.isOn {
                Button { model.asking = true } label: { Label(TidyText.freeTitle, systemImage: "memorychip") }
            }
            Button(role: .destructive) { askingErase = true } label: { Label(TidyText.eraseTitle, systemImage: "trash") }
        } label: {
            label()
        }
        .accessibilityLabel("\(TidyText.freeTitle) or \(TidyText.eraseTitle.lowercased())")
        .modifier(TidyFreeFlow(model: model, alertResult: true, onDone: {}))
        .modifier(TidyEraseFlow(chat: chat, asking: $askingErase, onClear: onClear, onFinished: {}))
    }
}
