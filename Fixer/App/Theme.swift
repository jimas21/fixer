import SwiftUI
import UIKit

/// Colours, sizes and motion shared by the chat and Make screens. Dark values come from the approved mock-ups;
/// each has a light counterpart so light mode stays readable.
enum Fx {
    private static func rgba(_ r: Double, _ g: Double, _ b: Double, _ a: Double = 1) -> UIColor {
        UIColor(red: r / 255, green: g / 255, blue: b / 255, alpha: a)
    }
    private static func adaptive(dark: UIColor, light: UIColor) -> Color {
        Color(UIColor { $0.userInterfaceStyle == .dark ? dark : light })
    }

    static let canvas = adaptive(dark: .black, light: UIColor.systemGroupedBackground)
    static let glass = adaptive(dark: rgba(44, 44, 46, 0.74), light: rgba(250, 250, 252, 0.78))
    static let panel = adaptive(dark: rgba(38, 38, 40, 0.94), light: rgba(246, 246, 249, 0.94))
    static let bar = adaptive(dark: rgba(28, 28, 30, 0.88), light: rgba(250, 250, 252, 0.90))
    static let barSoft = adaptive(dark: rgba(28, 28, 30, 0.84), light: rgba(250, 250, 252, 0.86))
    static let hairline = adaptive(dark: UIColor(white: 1, alpha: 0.12), light: UIColor(white: 0, alpha: 0.10))
    static let bubbleMe = adaptive(dark: rgba(10, 132, 255, 0.24), light: rgba(10, 132, 255, 0.16))
    static let bubbleFixer = adaptive(dark: rgba(28, 28, 30), light: rgba(236, 236, 240))
    static let card = adaptive(dark: rgba(28, 28, 30), light: rgba(236, 236, 240))
    static let tile = adaptive(dark: UIColor(white: 1, alpha: 0.08), light: UIColor(white: 0, alpha: 0.06))
    static let fieldWell = adaptive(dark: UIColor(white: 0, alpha: 0.38), light: UIColor(white: 0, alpha: 0.06))
    static let accent = Color.accentColor
    static let accentText = adaptive(dark: rgba(90, 169, 255), light: rgba(0, 98, 204))
    static let accentSoftText = adaptive(dark: rgba(156, 201, 255), light: rgba(0, 98, 204))
    static let onTint = adaptive(dark: rgba(10, 132, 255, 0.18), light: rgba(10, 132, 255, 0.14))
    static let onTintStrong = adaptive(dark: rgba(10, 132, 255, 0.24), light: rgba(10, 132, 255, 0.18))
    static let explicitStroke = Color(red: 1, green: 0.62, blue: 0.04)
    static let explicitText = adaptive(dark: rgba(255, 194, 102), light: rgba(168, 92, 0))
    static let explicitFill = adaptive(dark: rgba(255, 159, 10, 0.22), light: rgba(255, 159, 10, 0.20))
    static let explicitSwitch = adaptive(dark: rgba(58, 40, 14, 0.92), light: rgba(255, 244, 224, 0.95))
    static let danger = adaptive(dark: rgba(255, 105, 97), light: rgba(215, 0, 21))
    static let secondaryText = adaptive(dark: rgba(174, 174, 178), light: rgba(99, 99, 102))
    static let placeholder = adaptive(dark: rgba(142, 142, 147), light: rgba(142, 142, 147))
    static let tabIdle = adaptive(dark: rgba(209, 209, 214), light: rgba(99, 99, 102))
    static let chipX = adaptive(dark: rgba(72, 72, 74), light: rgba(174, 174, 178))
    static let grabber = adaptive(dark: rgba(99, 99, 102), light: rgba(174, 174, 178))
    static let segment = adaptive(dark: rgba(72, 72, 74), light: rgba(255, 255, 255))
    static let switchTrack = adaptive(dark: rgba(99, 99, 102), light: rgba(174, 174, 178))
    /// The round Send / Stop button and the big Create button.
    static let solid = adaptive(dark: .white, light: .black)
    static let onSolid = adaptive(dark: .black, light: .white)
    static let solidSoft = adaptive(dark: UIColor(white: 1, alpha: 0.13), light: UIColor(white: 0, alpha: 0.10))
    static let wellStroke = adaptive(dark: UIColor(white: 1, alpha: 0.12), light: UIColor(white: 0, alpha: 0.12))

    static let composerRadius: CGFloat = 26
    static let panelRadius: CGFloat = 34
    static let tabBarRadius: CGFloat = 31
    static let promptRadius: CGFloat = 20
    static let tileRadius: CGFloat = 14
    static let cardRadius: CGFloat = 18
    static let bubbleRadius: CGFloat = 18
}

/// A rounded glass surface: material, a tint over it and a hairline edge.
struct GlassSurface: ViewModifier {
    var radius: CGFloat
    var tint: Color

    func body(content: Content) -> some View {
        content
            .background {
                ZStack {
                    RoundedRectangle(cornerRadius: radius, style: .continuous).fill(.ultraThinMaterial)
                    RoundedRectangle(cornerRadius: radius, style: .continuous).fill(tint)
                }
            }
            .overlay {
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .strokeBorder(Fx.hairline, lineWidth: 0.5)
            }
    }
}

extension View {
    func glass(radius: CGFloat, tint: Color) -> some View { modifier(GlassSurface(radius: radius, tint: tint)) }
}

/// The 44 pt round buttons in the headers.
struct GlassCircleButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 18, weight: .semibold))
            .foregroundStyle(.primary)
            .frame(width: 44, height: 44)
            .background(Circle().fill(Fx.bar))
            .overlay(Circle().strokeBorder(Fx.hairline, lineWidth: 0.5))
            .opacity(configuration.isPressed ? 0.7 : 1)
            .contentShape(Circle())
    }
}

/// A small capsule label: the Think / Enhance pills and chips. `style` picks the colours.
struct PillLabel: View {
    enum Style { case on, quiet, explicitOn }
    var title: String
    var systemImage: String?
    var style: Style = .on

    var body: some View {
        HStack(spacing: 5) {
            if let systemImage {
                Image(systemName: systemImage).font(.system(size: 14, weight: .semibold))
            }
            Text(title).font(.footnote.weight(.semibold)).lineLimit(1)
        }
        .padding(.horizontal, 11)
        .frame(minHeight: 30)
        .foregroundStyle(foreground)
        .background(Capsule().fill(fill))
    }

    private var foreground: Color {
        switch style {
        case .on: return Fx.accentSoftText
        case .quiet: return Fx.secondaryText
        case .explicitOn: return Fx.explicitText
        }
    }
    private var fill: Color {
        switch style {
        case .on: return Fx.onTint
        case .quiet: return Fx.tile
        case .explicitOn: return Fx.explicitFill
        }
    }
}

/// Shared springs. Everything falls back to a plain short fade when Reduce Motion is on (see `fxAnimation`).
enum Motion {
    static let snap = Animation.spring(response: 0.42, dampingFraction: 0.82)
    static let follow = Animation.interactiveSpring(response: 0.28, dampingFraction: 0.86)
    static let pop = Animation.spring(response: 0.32, dampingFraction: 0.86)
    static let bar = Animation.snappy(duration: 0.25)
    static let fade = Animation.easeInOut(duration: 0.25)
    static let reduced = Animation.easeInOut(duration: 0.2)
}

private struct FxAnimation<V: Equatable>: ViewModifier {
    @Environment(\.accessibilityReduceMotion) private var reduce
    let animation: Animation
    let value: V

    func body(content: Content) -> some View {
        content.animation(reduce ? Motion.reduced : animation, value: value)
    }
}

extension View {
    /// `.animation(_:value:)` that turns springs into a short fade when Reduce Motion is on.
    func fxAnimation<V: Equatable>(_ animation: Animation, value: V) -> some View {
        modifier(FxAnimation(animation: animation, value: value))
    }
}

extension Collection where Element == String {
    /// The non-empty strings joined with `separator`.
    func joinedNonEmpty(_ separator: String) -> String {
        filter { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }.joined(separator: separator)
    }
}
