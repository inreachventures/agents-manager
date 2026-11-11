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
    private(set) var pid: pid_t = 0  // the tmux client's process
    private(set) var startedAt = Date.distantPast
    var environment: [String] = []
    /// Called when the client went away by itself (Claude exited, the session was archived); not on `detach()`.
    var onDetached: ((String) -> Void)?

    override init() {
        super.init()
        view.processDelegate = self
        // Shift+Enter: a new line in Claude's prompt rather than submitting it. SwiftTerm sends a plain CR for it,
        // the same as Enter, and its key handling can't be overridden (public, not open), so catch it before it
        // gets there and send ESC CR (Meta+Enter), which Claude Code reads as a newline and tmux passes through.
        NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard Self.isShiftReturn(event) else { return event }
            // local monitors run on the main thread
            let target = event.window
            let handled = MainActor.assumeIsolated { () -> Bool in
                guard let self, let window = self.view.window, target === window,
                      window.firstResponder === self.view, self.isAttached,
                      !self.view.hasMarkedText() else { return false }  // an IME composing: let it commit
                self.view.send(data: [0x1b, 0x0d])
                return true
            }
            return handled ? nil : event
        }
    }

    nonisolated static func isShiftReturn(_ event: NSEvent) -> Bool {
        let returnKeys: Set<UInt16> = [36, 76]  // Return, keypad Enter
        let mods = event.modifierFlags.intersection([.shift, .control, .option, .command])
        return returnKeys.contains(event.keyCode) && mods == .shift
    }

    var isAttached: Bool { view.process.running }

    func show(_ key: String, backend: Backend) async throws {
        if isAttached {
            if self.key == key { return }
            let result: SwitchResult = try await backend.call("switch", ["key": key, "pid": Int(pid)])
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
        guard view.process.running else { throw BackendError(message: "Couldn't start the terminal (\(exe))") }
        pid = view.process.shellPid
        startedAt = Date()
        self.key = key
    }

    /// Show `key` again at the next `show`, e.g. after tmux moved the client to another session (C-b s).
    func forget() {
        key = nil
    }

    /// Stop showing any session (the tmux client exits; the session keeps running).
    func detach() {
        key = nil
        guard isAttached else { return }
        let old = pid
        pid = 0
        view.terminate()  // SIGTERM; SwiftTerm stops watching the process, so no processTerminated follows
        DispatchQueue.global(qos: .utility).async {
            var status: Int32 = 0
            waitpid(old, &status, 0)  // reap it
        }
    }

    func focus() {
        view.window?.makeFirstResponder(view)
    }

    // MARK: LocalProcessTerminalViewDelegate

    nonisolated func sizeChanged(source: LocalProcessTerminalView, newCols: Int, newRows: Int) {}
    nonisolated func setTerminalTitle(source: LocalProcessTerminalView, title: String) {}
    nonisolated func hostCurrentDirectoryUpdate(source: TerminalView, directory: String?) {}

    nonisolated func processTerminated(source: TerminalView, exitCode: Int32?) {
        // SwiftTerm reports on the main queue: check right away which process this is about
        MainActor.assumeIsolated {
            guard pid != 0, view.process.shellPid == pid else { return }  // not the current client
            let gone = key
            key = nil
            pid = 0
            if let gone { onDetached?(gone) }
        }
    }
}

final class WMTerminalView: LocalProcessTerminalView {
    override init(frame: CGRect) {
        super.init(frame: frame)
        font = NSFont.monospacedSystemFont(ofSize: 12.5, weight: .regular)
        // off by default, as in Terminal.app: many layouts type @ # [ ] { } with Option
        optionAsMetaKey = UserDefaults.standard.bool(forKey: "optionAsMeta")
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
