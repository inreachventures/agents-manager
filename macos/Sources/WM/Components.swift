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
                Pulse(color: .orange, symbol: "exclamationmark", period: 1.0)
            case .yourTurn:
                Pulse(color: .teal, symbol: nil, period: 1.8)
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

private struct Pulse: View {
    var color: Color
    var symbol: String?
    var period: Double

    var body: some View {
        TimelineView(.animation(minimumInterval: 1 / 30)) { context in
            let t = context.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: period) / period
            ZStack {
                Circle()
                    .fill(color.opacity(0.45 * (1 - t)))
                    .frame(width: 18, height: 18)
                    .scaleEffect(0.45 + 0.65 * t)
                Circle().fill(color).frame(width: symbol == nil ? 8 : 12, height: symbol == nil ? 8 : 12)
                if let symbol {
                    Image(systemName: symbol).font(.system(size: 7, weight: .black)).foregroundStyle(.white)
                }
            }
        }
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
