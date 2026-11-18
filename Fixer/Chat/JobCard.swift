import SwiftUI

/// One piece of computer work this chat started, drawn as a card about the size of the picture it will become:
/// the live preview (or the photo being changed), a spinner, what it is doing, the line, and Stop for this job.
struct JobCard: View {
    let job: Chat.Job
    let stop: () -> Void
    let showLine: () -> Void
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var sweep = false

    private let width: CGFloat = 250

    private var shown: UIImage? { job.preview ?? job.base }

    private var pictureHeight: CGFloat {
        guard let shown, shown.size.width > 0, shown.size.height > 0 else { return 180 }
        let h = width / (shown.size.width / shown.size.height)
        return min(300, max(160, h))
    }

    var body: some View {
        HStack {
            VStack(spacing: 0) {
                picture
                    .frame(width: width, height: pictureHeight)
                    .clipped()
                    .overlay(alignment: .bottom) {
                        if let p = job.progress {
                            ProgressView(value: min(1, max(0, p)))
                                .progressViewStyle(.linear)
                                .tint(Fx.accent)
                        }
                    }
                HStack(spacing: 10) {
                    ProgressView().controlSize(.small)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(job.title).font(.subheadline.weight(.semibold)).lineLimit(1)
                        Text(job.status).font(.footnote).foregroundStyle(Fx.secondaryText).lineLimit(2)
                    }
                    Spacer(minLength: 0)
                    Button(action: showLine) {
                        Image(systemName: "list.number").font(.title3).foregroundStyle(Fx.secondaryText).iconButton()
                    }
                    .accessibilityLabel("See what's running on your computer")
                    Button(action: stop) {
                        Image(systemName: "stop.circle.fill").font(.title3).foregroundStyle(Fx.secondaryText).iconButton()
                    }
                    .accessibilityLabel("Stop this")
                }
                .padding(.leading, 14).padding(.trailing, 2).padding(.vertical, 2)
            }
            .frame(width: width)
            .background(Fx.card)
            .clipShape(RoundedRectangle(cornerRadius: Fx.cardRadius, style: .continuous))
            .overlay {
                RoundedRectangle(cornerRadius: Fx.cardRadius, style: .continuous).strokeBorder(Fx.hairline, lineWidth: 0.5)
            }
            Spacer(minLength: 40)
        }
        .transition(.opacity)
        .accessibilityElement(children: .contain)
    }

    @ViewBuilder private var picture: some View {
        if let preview = job.preview {
            Image(uiImage: preview).resizable().scaledToFill()
        } else if let base = job.base {
            Image(uiImage: base).resizable().scaledToFill()
                .overlay(Color.black.opacity(0.38))
                .overlay { highlightBand }
        } else {
            ZStack {
                Fx.tile
                Image(systemName: "photo").font(.system(size: 30)).foregroundStyle(Fx.secondaryText)
            }
            .overlay { highlightBand }
        }
    }

    /// A soft band that slides down the picture while it waits. Still under Reduce Motion.
    private var highlightBand: some View {
        GeometryReader { geo in
            LinearGradient(colors: [.clear, Color.white.opacity(0.10), .clear], startPoint: .top, endPoint: .bottom)
                .frame(height: 44)
                .offset(y: sweep ? geo.size.height : -44)
                .onAppear {
                    guard !reduceMotion else { return }
                    withAnimation(.linear(duration: 1.6).repeatForever(autoreverses: false)) { sweep = true }
                }
        }
        .allowsHitTesting(false)
    }
}

/// The thinking line: what Fixer is doing before a picture starts (thinking, writing the prompt, searching). Its Stop
/// is the composer's round button.
struct WorkingRow: View {
    let text: String
    let progress: Double?
    var showLine: (() -> Void)? = nil

    var body: some View {
        HStack {
            HStack(alignment: .center, spacing: 10) {
                ProgressView().controlSize(.small)
                VStack(alignment: .leading, spacing: 4) {
                    Text(text).font(.subheadline)
                    if let progress { ProgressView(value: progress).frame(width: 160) }
                }
                if let showLine {
                    Button(action: showLine) {
                        Image(systemName: "list.number").font(.title3).foregroundStyle(Fx.secondaryText).iconButton()
                    }
                    .accessibilityLabel("See what's running on your computer")
                }
            }
            .padding(.horizontal, 12).padding(.vertical, 6)
            .background(Fx.bubbleFixer)
            .clipShape(RoundedRectangle(cornerRadius: Fx.bubbleRadius, style: .continuous))
            Spacer(minLength: 40)
        }
    }
}
