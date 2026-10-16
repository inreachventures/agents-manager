#if DEBUG
import AppKit
import SwiftUI

/// Development builds only. With WM_DEBUG_DIR set, the app runs the commands written to $WM_DEBUG_DIR/cmd, one per
/// line, so a script can drive it and take snapshots of its own windows (no screen-recording permission needed):
///   snap NAME · select KEY · select-archived KEY · select-repo KEY REPO · select-row N · toggle-repo REPO
///   changes on|off · archived on|off · collapse-all · expand-all · sheet new|add|rebase|archive · key return|escape
///   click X Y (from the top left) · create [TICKET] · resume · restore · restart · dump · size W H · offscreen
///   appearance dark|light
@MainActor
enum DebugControl {
    private static var timer: Timer?

    static func start(model: AppModel) {
        guard timer == nil, let dir = ProcessInfo.processInfo.environment["WM_DEBUG_DIR"] else { return }
        if let look = ProcessInfo.processInfo.environment["WM_DEBUG_APPEARANCE"] {
            NSApp.appearance = NSAppearance(named: look == "light" ? .aqua : .darkAqua)
        }
        let cmd = URL(fileURLWithPath: dir).appendingPathComponent("cmd")
        timer = Timer.scheduledTimer(withTimeInterval: 0.3, repeats: true) { _ in
            MainActor.assumeIsolated {
                guard let text = try? String(contentsOf: cmd, encoding: .utf8), !text.isEmpty else { return }
                try? "".write(to: cmd, atomically: true, encoding: .utf8)
                for line in text.split(separator: "\n") {
                    run(line.split(separator: " ").map(String.init), model: model, dir: dir)
                }
            }
        }
    }

    private static var window: NSWindow? {
        NSApp.windows.first { $0.isVisible && !($0 is NSPanel) && $0.sheetParent == nil }
    }

    private static func run(_ args: [String], model: AppModel, dir: String) {
        guard let command = args.first else { return }
        let rest = Array(args.dropFirst())
        switch command {
        case "snap":
            guard let window else { return }
            let name = rest.first ?? "snap"
            snapshot(window, to: "\(dir)/\(name).png")
            if let sheet = window.attachedSheet { snapshot(sheet, to: "\(dir)/\(name)-sheet.png") }
        case "select":
            model.showArchived = false
            model.selection = .workstream(rest[0])
        case "select-archived":
            model.showArchived = true
            model.selection = .workstream(rest[0])
        case "select-repo":
            model.selection = .repo(rest[0], rest[1])
        case "changes":
            model.showChanges = rest.first == "on"
        case "archived":
            model.showArchived = rest.first == "on"
        case "collapse-all":
            model.collapseAll()
        case "expand-all":
            model.expandAll()
        case "sheet":
            guard let key = model.selected?.key else { return }
            switch rest.first {
            case "new": model.sheet = .new
            case "add": Task { await model.startAddRepo(key) }
            case "rebase": Task { await model.startRebase(key) }
            case "archive": Task { await model.startArchive(key) }
            default: model.sheet = nil
            }
        case "restore":
            if let key = model.selected?.key { Task { await model.restore(key) } }
        case "resume":
            if let key = model.selected?.key { Task { await model.resume(key) } }
        case "select-row":  // as a click on sidebar row N would: through the table view, so tags are exercised
            guard let table = window.flatMap({ firstTable(in: $0.contentView) }), let row = Int(rest[0]) else {
                return
            }
            table.selectRowIndexes(IndexSet(integer: row), byExtendingSelection: false)
            let selected = model.selection.map { "\($0)" } ?? "nil"
            let out = "rows=\(table.numberOfRows) class=\(type(of: table)) selected=\(selected)\n"
            try? out.write(toFile: "\(dir)/out", atomically: true, encoding: .utf8)
        case "offscreen":  // out of the way of real clicks; the window server still captures it
            window?.setFrameOrigin(NSPoint(x: -5000, y: 200))
        case "create":
            Task { await model.create(ticket: rest.first ?? "") }
        case "dump":
            let out = ["selection=\(model.selection.map { "\($0)" } ?? "nil")",
                       "selected=\(model.selected?.key ?? "nil") terminal=\(model.terminal.key ?? "nil")",
                       "attached=\(model.terminal.isAttached) busy=\(model.busy ?? "-")",
                       "rows=\(model.state.workstreams.map(\.key))"].joined(separator: "\n")
            try? (out + "\n").write(toFile: "\(dir)/out", atomically: true, encoding: .utf8)
        case "toggle-repo":
            if let key = model.selected?.key { model.toggleRepo(key, rest[0]) }
        case "restart":
            model.restart()
        case "toast":
            model.toast(rest.joined(separator: " "), .info)
        case "appearance":
            NSApp.appearance = NSAppearance(named: rest.first == "light" ? .aqua : .darkAqua)
        case "size":
            window?.setContentSize(NSSize(width: Double(rest[0]) ?? 1200, height: Double(rest[1]) ?? 760))
        case "key":  // to the sheet if one is open: return | escape
            guard let window, let target = window.attachedSheet ?? Optional(window) else { return }
            let (chars, code): (String, UInt16) = rest.first == "escape" ? ("\u{1b}", 53) : ("\r", 36)
            for type in [NSEvent.EventType.keyDown, .keyUp] {
                if let event = NSEvent.keyEvent(with: type, location: .zero, modifierFlags: [],
                                                timestamp: ProcessInfo.processInfo.systemUptime,
                                                windowNumber: target.windowNumber, context: nil, characters: chars,
                                                charactersIgnoringModifiers: chars, isARepeat: false, keyCode: code) {
                    target.sendEvent(event)
                }
            }
        case "click":
            guard let window, let x = Double(rest[0]), let y = Double(rest[1]) else { return }
            let point = NSPoint(x: x, y: window.frame.height - y)
            for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
                if let event = NSEvent.mouseEvent(with: type, location: point, modifierFlags: [],
                                                  timestamp: ProcessInfo.processInfo.systemUptime,
                                                  windowNumber: window.windowNumber, context: nil, eventNumber: 0,
                                                  clickCount: 1, pressure: 1) {
                    NSApp.postEvent(event, atStart: false)  // queued: a table's mouse-down loop waits for the up
                }
            }
        default:
            break
        }
    }

    private typealias CreateImage = @convention(c) (CGRect, UInt32, UInt32, UInt32) -> Unmanaged<CGImage>?

    /// Through the window server, so materials render as on screen. An app may capture its own windows without
    /// screen-recording permission; the function is looked up at run time because newer SDKs hide it.
    private static func firstTable(in view: NSView?) -> NSTableView? {
        guard let view else { return nil }
        if let table = view as? NSTableView { return table }
        for sub in view.subviews { if let table = firstTable(in: sub) { return table } }
        return nil
    }

    private static func snapshot(_ window: NSWindow, to path: String) {
        guard let sym = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "CGWindowListCreateImage") else { return }
        let create = unsafeBitCast(sym, to: CreateImage.self)
        // .optionIncludingWindow = 1 << 3, .boundsIgnoreFraming | .bestResolution = 1 | 1 << 3
        guard let image = create(.null, 1 << 3, UInt32(window.windowNumber), 1 | 1 << 3)?.takeRetainedValue() else {
            return
        }
        let rep = NSBitmapImageRep(cgImage: image)
        try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
    }
}
#endif
