import AppKit
import SwiftTerm
import SwiftUI

/// One terminal for the whole window: a tmux client on the selected workstream's app view (`<key>_app`, see
/// wm's tmux.py). Selecting another workstream switches that client instead of starting a new one, so exactly the
/// session on screen counts as "being looked at" when wm decides whether to send a notification.
@MainActor
final class TerminalController: NSObject, LocalProcessTerminalViewDelegate {
    let view = WMTerminalView(frame: NSRect(x: 0, y: 0, width: 800, height: 600))
    private(set) var key: String?  // the workstream the client shows
    var environment: [String] = []
    /// Called when the client went away by itself (Claude exited, the session was archived); not on `detach()`.
    var onDetached: ((String) -> Void)?
    private var detaching = false

    override init() {
        super.init()
        view.processDelegate = self
    }

    var isAttached: Bool { view.process.running }

    func show(_ key: String, backend: Backend) async throws {
        if isAttached {
            if self.key == key { return }
            let pid = Int(view.process.shellPid)
            let result: SwitchResult = try await backend.call("switch", ["key": key, "pid": pid])
            if result.switched {
                self.key = key
                return
            }
            detach()
        }
        let attach: AttachResult = try await backend.call("attach", ["key": key])
        guard let exe = attach.argv.first else { return }
        view.terminal.resetToInitialState()
        view.startProcess(executable: exe, args: Array(attach.argv.dropFirst()), environment: environment,
                          execName: nil, currentDirectory: NSHomeDirectory())
        self.key = key
    }

    /// Stop showing any session (the tmux client exits; the session keeps running).
    func detach() {
        guard isAttached else { key = nil; return }
        detaching = true
        view.terminate()
        key = nil
    }

    func focus() {
        view.window?.makeFirstResponder(view)
    }

    // MARK: LocalProcessTerminalViewDelegate

    nonisolated func sizeChanged(source: LocalProcessTerminalView, newCols: Int, newRows: Int) {}
    nonisolated func setTerminalTitle(source: LocalProcessTerminalView, title: String) {}
    nonisolated func hostCurrentDirectoryUpdate(source: TerminalView, directory: String?) {}

    nonisolated func processTerminated(source: TerminalView, exitCode: Int32?) {
        DispatchQueue.main.async {
            MainActor.assumeIsolated {
                let gone = self.key
                self.key = nil
                if self.detaching {
                    self.detaching = false
                } else if let gone {
                    self.onDetached?(gone)
                }
            }
        }
    }
}

final class WMTerminalView: LocalProcessTerminalView {
    override init(frame: CGRect) {
        super.init(frame: frame)
        font = NSFont.monospacedSystemFont(ofSize: 12.5, weight: .regular)
        optionAsMetaKey = true
        applyColors()
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyColors()
    }

    static let background = NSColor(name: nil) { appearance in
        appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            ? NSColor(srgbRed: 0.11, green: 0.11, blue: 0.12, alpha: 1) : .white
    }

    private func applyColors() {
        let dark = effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        effectiveAppearance.performAsCurrentDrawingAppearance {
            nativeBackgroundColor = Self.background.usingColorSpace(.sRGB) ?? .black
        }
        nativeForegroundColor = dark ? NSColor(white: 0.9, alpha: 1) : NSColor(white: 0.12, alpha: 1)
        caretColor = .controlAccentColor
        selectedTextBackgroundColor = NSColor.selectedTextBackgroundColor
    }
}

struct TerminalHost: NSViewRepresentable {
    let controller: TerminalController

    func makeNSView(context: Context) -> NSView {
        let container = NSView()
        container.wantsLayer = true
        embed(in: container)
        return container
    }

    func updateNSView(_ nsView: NSView, context: Context) {
        if controller.view.superview !== nsView { embed(in: nsView) }
    }

    private func embed(in container: NSView) {
        let term = controller.view
        term.removeFromSuperview()
        term.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(term)
        NSLayoutConstraint.activate([
            term.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 8),
            term.trailingAnchor.constraint(equalTo: container.trailingAnchor),
            term.topAnchor.constraint(equalTo: container.topAnchor, constant: 4),
            term.bottomAnchor.constraint(equalTo: container.bottomAnchor),
        ])
        DispatchQueue.main.async { term.window?.makeFirstResponder(term) }
    }
}
