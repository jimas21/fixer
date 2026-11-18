import SwiftUI
import UIKit

/// The chat's one soft rounded composer. Top line (only when there is something to show): the working photo as a
/// small thumbnail. Then the field across the whole width. Bottom row: +, the model, small Think and Enhance buttons
/// while they are on, and at the far right one round button that is Send, and Stop while this chat's work runs.
struct QuietComposer: View {
    @ObservedObject var chat: Chat
    @Binding var input: String
    @Binding var attachment: UIImage?
    var typing: FocusState<Bool>.Binding
    let onPhotos: () -> Void
    let onCamera: () -> Void
    let onQueue: () -> Void
    let onOpenPhoto: () -> Void
    let onSend: () -> Void

    @ObservedObject private var rules = Rules.shared
    @ObservedObject private var desktop = Desktop.shared
    @AppStorage("chat.enhance") private var chatEnhance = true
    @State private var stopTick = 0

    private var typed: Bool { !input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || attachment != nil }
    private var minePending: Bool { chat.jobs.contains { $0.conversation == chat.conversation.id } }
    private var elsewhere: Int { chat.jobs.filter { $0.conversation != chat.conversation.id }.count }
    private var showThink: Bool { desktop.isOn && rules.thinkMode }
    private var showEnhance: Bool { desktop.isOn && chatEnhance }
    private var workingImage: UIImage? { attachment ?? chat.photo }
    private var showTopLine: Bool { workingImage != nil || elsewhere > 0 }

    /// The one button's job, by the rule in the plan: thinking -> Stop; text typed -> Send; only computer work -> Stop.
    private enum ButtonMode { case send, stop, idle }
    private var mode: ButtonMode {
        if chat.busy != nil { return .stop }
        if typed { return chat.canSend ? .send : .idle }
        if minePending || chat.phoneWork { return .stop }
        return .idle
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if showTopLine {
                topLine
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
            field
            controls
        }
        .padding(.top, 6).padding(.horizontal, 6).padding(.bottom, 4)
        .glass(radius: Fx.composerRadius, tint: Fx.glass)
        .padding(.horizontal, 8)
        .padding(.bottom, 8)
        .fxAnimation(Motion.pop, value: showTopLine)
        .sensoryFeedback(.impact(weight: .light), trigger: stopTick)
    }

    // MARK: top line

    private var topLine: some View {
        HStack(spacing: 10) {
            if let image = workingImage {
                ComposerPhotoChip(image: image, ringed: attachment == nil && chat.mask != nil,
                                  isAttachment: attachment != nil,
                                  canPutAway: attachment != nil || chat.busy == nil,
                                  open: { if attachment != nil { typing.wrappedValue = true } else { onOpenPhoto() } },
                                  remove: { if attachment != nil { attachment = nil } else { chat.closePhoto() } })
                Text(attachment != nil ? "Photo to send" : chat.mask != nil ? "Area painted" : "This photo")
                    .font(.footnote)
                    .foregroundStyle(Fx.secondaryText)
                    .lineLimit(1)
            }
            if elsewhere > 0 {
                Button(action: onQueue) {
                    PillLabel(title: "\(elsewhere) from other chats", systemImage: "list.number", style: .quiet)
                        .frame(minHeight: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Working on \(elsewhere) \(elsewhere == 1 ? "thing" : "things") from another chat")
                .accessibilityHint("Opens what's running on your computer")
            }
            Spacer(minLength: 0)
        }
        .padding(.leading, 8).padding(.trailing, 4)
        .frame(minHeight: 48)
    }

    // MARK: field

    /// The words get the whole width of the box.
    private var field: some View {
        TextField(chat.photo != nil || attachment != nil ? "Say what to change" : "Message Fixer",
                  text: $input, axis: .vertical)
            .lineLimit(1...6)
            .font(.body)
            .submitLabel(.return)
            .focused(typing)
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .background {
                // Behind the field, so a tap on the padding focuses it without getting in the way of the cursor.
                Color.clear
                    .contentShape(Rectangle())
                    .onTapGesture { typing.wrappedValue = true }
                    .accessibilityHidden(true)
            }
    }

    // MARK: controls

    private var controls: some View {
        HStack(spacing: 0) {
            ComposerPlusMenu(onPhotos: onPhotos, onCamera: onCamera, onQueue: onQueue)
            ModelChip()
            if showThink {
                StatePill(systemImage: "brain",
                          label: "Think is on. Tap to turn it off") { rules.thinkMode = false }
                    .transition(.scale(scale: 0.9).combined(with: .opacity))
            }
            if showEnhance {
                StatePill(systemImage: "sparkles",
                          label: "Enhance my prompt is on. Tap to turn it off") { chatEnhance = false }
                    .transition(.scale(scale: 0.9).combined(with: .opacity))
            }
            Spacer(minLength: 0)
            SendStopButton(isStop: mode == .stop, enabled: mode != .idle, action: primary)
        }
        .fxAnimation(Motion.pop, value: showThink)
        .fxAnimation(Motion.pop, value: showEnhance)
    }

    private func primary() {
        switch mode {
        case .send:
            onSend()
        case .stop:
            stopTick += 1
            if chat.busy != nil { chat.stop() }
            else if minePending { chat.stopThisChat() }
            else { chat.stop() }
        case .idle:
            break
        }
    }
}

/// The working photo (or the one about to be sent) as a 48 pt thumbnail, with a small x to put it away.
struct ComposerPhotoChip: View {
    let image: UIImage
    let ringed: Bool
    let isAttachment: Bool
    let canPutAway: Bool
    let open: () -> Void
    let remove: () -> Void

    var body: some View {
        Button(action: open) {
            Image(uiImage: image)
                .resizable()
                .scaledToFill()
                .frame(width: 48, height: 48)
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                .overlay {
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .strokeBorder(Fx.accent, lineWidth: ringed ? 2 : 0)
                }
                .frame(minWidth: 48, minHeight: 48)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(isAttachment ? "Photo to send" : "This photo")
        .accessibilityHint(isAttachment ? "Opens the message box" : "Opens this photo to paint or save")
        .overlay(alignment: .topTrailing) {
            Button(action: remove) {
                ZStack(alignment: .bottomLeading) {
                    Color.clear
                    Circle()
                        .fill(Fx.chipX)
                        .frame(width: 20, height: 20)
                        .overlay(Image(systemName: "xmark").font(.system(size: 10, weight: .bold)).foregroundStyle(.white))
                }
                .frame(width: 44, height: 44)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .opacity(canPutAway ? 1 : 0.45)
            .disabled(!canPutAway)
            .offset(x: 34, y: -34)
            .accessibilityLabel(isAttachment ? "Remove the photo" : "Put this photo away")
        }
    }
}

/// A small round Think / Enhance button: shown only while it is on; tap turns it off.
struct StatePill: View {
    let systemImage: String
    let label: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            ZStack {
                Circle().fill(Fx.onTint).frame(width: 36, height: 36)
                Image(systemName: systemImage)
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Fx.accentSoftText)
            }
            .frame(width: 44, height: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }
}

/// The + menu: add a photo, take one, Think, Enhance my prompt, Queue.
struct ComposerPlusMenu: View {
    let onPhotos: () -> Void
    let onCamera: () -> Void
    let onQueue: () -> Void
    @ObservedObject private var rules = Rules.shared
    @ObservedObject private var desktop = Desktop.shared
    @AppStorage("chat.enhance") private var chatEnhance = true

    var body: some View {
        Menu {
            Button { onPhotos() } label: { Label("Photos", systemImage: "photo.on.rectangle") }
            if UIImagePickerController.isSourceTypeAvailable(.camera) {
                Button { onCamera() } label: { Label("Camera", systemImage: "camera") }
            }
            if desktop.isOn {
                Toggle(isOn: $rules.thinkMode) { Label("Think", systemImage: "brain") }
                Toggle(isOn: $chatEnhance) { Label("Enhance my prompt", systemImage: "sparkles") }
                Divider()
                Button { onQueue() } label: { Label("Queue", systemImage: "list.number") }
            }
        } label: {
            ZStack {
                Circle().fill(Fx.tile).frame(width: 36, height: 36)
                Image(systemName: "plus")
                    .font(.system(size: 17, weight: .medium))
                    .foregroundStyle(.primary)
            }
            .frame(width: 44, height: 44)
            .contentShape(Rectangle())
        }
        .accessibilityLabel("Add a photo and more")
    }
}

/// The one round button. Send is a white circle with an arrow; Stop is the same circle with a square. One frame
/// both ways, so nothing shifts when it changes.
struct SendStopButton: View {
    let isStop: Bool
    let enabled: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            ZStack {
                Circle().fill(Fx.solid).frame(width: 36, height: 36)
                Image(systemName: isStop ? "stop.fill" : "arrow.up")
                    .font(.system(size: isStop ? 13 : 16, weight: .bold))
                    .foregroundStyle(Fx.onSolid)
                    .contentTransition(.symbolEffect(.replace))
            }
            .frame(width: 44, height: 44)
            .contentShape(Rectangle())
            .opacity(enabled ? 1 : 0.4)
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .accessibilityLabel(isStop ? "Stop" : "Send")
        .accessibilityHint(isStop ? "Stops what this chat is doing" : "")
    }
}
