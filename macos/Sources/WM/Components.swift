import AppKit
import SwiftUI

extension Color {
    /// wm's state colours (view.git_state), readable in light and dark mode.
    static func tone(_ name: String) -> Color {
        switch name {
        case "red": return .red
        case "yellow": return .amber
        case "blue": return .blue
        case "green": return .green
        case "cyan": return .teal
        case "orange": return .orange
        case "purple": return .purple
        case "grey": return .secondary
        default: return .primary
        }
    }

    static let amber = Color(nsColor: NSColor(name: nil) { appearance in
        appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            ? NSColor.systemYellow : NSColor(srgbRed: 0.70, green: 0.47, blue: 0.0, alpha: 1)
    })

    static func status(_ status: Status) -> Color {
        switch status {
        case .needsYou: return .orange
        case .working: return .green
        case .yourTurn: return .teal
        default: return .secondary
        }
    }
}

/// "3m", "2h", "5d", as in the TUI.
func ago(_ timestamp: Double, now: Date = Date()) -> String {
    let s = max(0, Int(now.timeIntervalSince1970 - timestamp))
    for (unit, n) in [("d", 86400), ("h", 3600), ("m", 60)] where s >= n {
        return "\(s / n)\(unit)"
    }
    return "\(s)s"
}

/// Spinner while Claude works; a pulse while it waits for you (stronger when blocked on a question or permission).
struct StatusIndicator: View {
    var status: Status

    var body: some View {
        Group {
            switch status {
            case .working:
                ProgressView().controlSize(.small).scaleEffect(0.75)
            case .needsYou:
                Pulse(color: .systemOrange, bang: true, period: 1.0)
            case .yourTurn:
                Pulse(color: .systemTeal, bang: false, period: 1.8)
            case .archived:
                Image(systemName: "archivebox").font(.system(size: 10)).foregroundStyle(.tertiary)
            default:
                Circle().strokeBorder(Color.secondary.opacity(0.6), lineWidth: 1.2).frame(width: 8, height: 8)
            }
        }
        .frame(width: 18, height: 18)
        .help(status.help)
    }
}

/// A dot with a ring that grows and fades. Core Animation runs it in the render server, so it costs the app nothing per
/// frame however many workstreams wait (a SwiftUI animation re-rendered their rows 30 times a second, which made the
/// terminal lag).
private struct Pulse: NSViewRepresentable {
    var color: NSColor
    var bang: Bool  // "!" on a bigger dot: blocked on you
    var period: Double

    func makeNSView(context: Context) -> PulseView { PulseView() }

    func updateNSView(_ view: PulseView, context: Context) {
        view.configure(color: color, bang: bang, period: period)
    }
}

final class PulseView: NSView {
    private let ring = CALayer()
    private let dot = CALayer()
    private let mark = CAShapeLayer()
    private var color = NSColor.systemTeal
    private var bang = false
    private var period = 1.8

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        for sub in [ring, dot, mark] { layer?.addSublayer(sub) }
        mark.fillColor = .white
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override var intrinsicContentSize: NSSize { NSSize(width: 18, height: 18) }
    override var isFlipped: Bool { true }

    func configure(color: NSColor, bang: Bool, period: Double) {
        guard color != self.color || bang != self.bang || period != self.period || ring.animation(forKey: "pulse") == nil
        else { return }
        (self.color, self.bang, self.period) = (color, bang, period)
        needsLayout = true
        updateColors()
        animate()
    }

    override func layout() {
        super.layout()
        let center = CGPoint(x: bounds.midX, y: bounds.midY)
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        ring.bounds = CGRect(x: 0, y: 0, width: 18, height: 18)
        ring.cornerRadius = 9
        ring.position = center
        let d: CGFloat = bang ? 12 : 8
        dot.bounds = CGRect(x: 0, y: 0, width: d, height: d)
        dot.cornerRadius = d / 2
        dot.position = center
        let path = CGMutablePath()  // "!" in a 12 pt dot
        path.addRoundedRect(in: CGRect(x: center.x - 0.8, y: center.y - 3.6, width: 1.6, height: 4.2),
                            cornerWidth: 0.8, cornerHeight: 0.8)
        path.addEllipse(in: CGRect(x: center.x - 0.85, y: center.y + 1.6, width: 1.7, height: 1.7))
        mark.path = bang ? path : nil
        CATransaction.commit()
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        updateColors()
    }

    override func viewDidChangeBackingProperties() {
        super.viewDidChangeBackingProperties()
        mark.contentsScale = window?.backingScaleFactor ?? 2
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        if window != nil { animate() }  // layers lose their animations when taken off screen
    }

    private func updateColors() {
        effectiveAppearance.performAsCurrentDrawingAppearance {
            ring.backgroundColor = color.cgColor
            dot.backgroundColor = color.cgColor
        }
    }

    private func animate() {
        let scale = CABasicAnimation(keyPath: "transform.scale")
        scale.fromValue = 0.45
        scale.toValue = 1.1
        let fade = CABasicAnimation(keyPath: "opacity")
        fade.fromValue = 0.45
        fade.toValue = 0
        let group = CAAnimationGroup()
        group.animations = [scale, fade]
        group.duration = period
        group.repeatCount = .infinity
        group.timingFunction = CAMediaTimingFunction(name: .easeOut)
        group.isRemovedOnCompletion = false
        ring.opacity = 0
        ring.add(group, forKey: "pulse")
    }
}

extension Status {
    var help: String {
        switch self {
        case .needsYou: return "Claude is blocked on you (a permission prompt or a question)"
        case .working: return "Claude is working"
        case .yourTurn: return "Claude finished: your turn"
        case .stopped: return "Session stopped; resume it to continue the same conversation"
        case .archived: return "Archived"
        case .unknown: return ""
        }
    }
}

struct Pill: View {
    var text: String
    var tone: Color

    var body: some View {
        Text(text)
            .font(.caption2.weight(.medium))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(tone.opacity(0.14), in: Capsule())
            .foregroundStyle(tone)
    }
}

struct LineCounts: View {
    var additions: Int
    var deletions: Int

    var body: some View {
        HStack(spacing: 4) {
            Text("+\(additions)").foregroundStyle(.green)
            Text("−\(deletions)").foregroundStyle(.red)
        }
        .font(.caption.monospacedDigit())
    }
}
