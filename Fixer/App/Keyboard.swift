import SwiftUI
import UIKit

/// Tells the screens whether the on-screen keyboard is up, so the floating tab bar can step aside and the
/// composer / Make panel can sit straight on the keyboard.
@MainActor
final class KeyboardObserver: ObservableObject {
    @Published private(set) var visible = false
    @Published private(set) var height: CGFloat = 0
    private var tokens: [NSObjectProtocol] = []

    init() {
        let center = NotificationCenter.default
        tokens.append(center.addObserver(forName: UIResponder.keyboardWillShowNotification, object: nil,
                                         queue: .main) { [weak self] note in
            let frame = (note.userInfo?[UIResponder.keyboardFrameEndUserInfoKey] as? CGRect) ?? .zero
            MainActor.assumeIsolated {
                self?.height = frame.height
                self?.visible = frame.height > 0
            }
        })
        tokens.append(center.addObserver(forName: UIResponder.keyboardWillHideNotification, object: nil,
                                         queue: .main) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.height = 0
                self?.visible = false
            }
        })
    }

    deinit {
        for t in tokens { NotificationCenter.default.removeObserver(t) }
    }
}

extension UIApplication {
    /// Puts the keyboard away from anywhere.
    static func dismissKeyboard() {
        shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
    }
}
