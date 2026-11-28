import AppKit
import SwiftUI

/// Where `wm.openDiff` sends the changes panel: a file of `repo` (`path` relative to it), at `startLine` if set.
struct DiffJump: Equatable {
    var repo: String
    var path: String
    var startLine: Int?
}

/// A plugin's view where the app shows it; `id` is "<plugin>/<view>".
struct PlacedView: Identifiable {
    var plugin: PluginInfo
    var view: PluginViewSpec
    var id: String { AppModel.viewID(plugin, view) }
}

/// Plugins: their views in the main area and the side panel, the events the server sends about them, and what their
/// pages ask of the app through `window.wm` (PluginWebView.swift).
extension AppModel {
    // MARK: - Views

    /// Every main view of every plugin that is on, in plugin order (only those have views).
    var mainViews: [PlacedView] { views(place: "main") }

    /// Every side-panel view of every plugin that is on.
    var panelViews: [PlacedView] { views(place: "panel") }

    private func views(place: String) -> [PlacedView] {
        plugins.flatMap { plugin in
            plugin.views.filter { $0.place == place }.map { PlacedView(plugin: plugin, view: $0) }
        }
    }

    /// A view by its "<plugin>/<view>" id, if a plugin that is on has it there.
    func pluginView(_ id: String, place: String) -> PlacedView? {
        views(place: place).first { $0.id == id }
    }

    nonisolated static func viewID(_ plugin: PluginInfo, _ view: PluginViewSpec) -> String {
        "\(plugin.name)/\(view.id)"
    }

    /// The main area: "terminal" or a main view's "<plugin>/<view>". The choice is kept for the app; Terminal shows
    /// while the plugin it names is gone (or not listed yet).
    var center: String {
        get { pluginView(centerChoice, place: "main") == nil ? "terminal" : centerChoice }
        set {
            keep(center)
            keep(newValue)
            centerChoice = newValue
            UserDefaults.standard.set(newValue, forKey: "center")
            if newValue == "terminal" { terminal.focus() }  // not left typing into a page that is hidden
            syncPluginViews()  // the chips' repo is now the other view's
        }
    }

    /// The side panel: "changes" or a panel view's "<plugin>/<view>", kept the same way.
    var panelTab: String {
        get { pluginView(panelTabChoice, place: "panel") == nil ? "changes" : panelTabChoice }
        set {
            keep(panelTab)
            keep(newValue)
            panelTabChoice = newValue
            UserDefaults.standard.set(newValue, forKey: "panelTab")
        }
    }

    /// A page that has been shown stays loaded (hidden) while its workstream is selected, so switching back to it
    /// doesn't reload it.
    private func keep(_ id: String) {
        if id != "terminal" && id != "changes" && !keptViews.contains(id) { keptViews.insert(id) }
    }

    /// Whether a plugin's main view fills the main area of the selected (active) workstream: the top bar's repo chips
    /// are then about that view, not the changes panel.
    var pluginCentred: Bool {
        guard let ws = selected, ws.status != .archived else { return false }
        return center != "terminal"
    }

    /// The repo the top bar's chips narrow to (`wm.context.repo`), nil for all: the centred main view's own pick, or
    /// else the changes panel's.
    var contextRepo: String? {
        guard pluginCentred else { return soloRepo }
        guard let key = selection, let repo = pluginRepo[key],
              selected?.repos.contains(where: { $0.repo == repo }) == true else { return nil }
        return repo
    }

    /// Show a view by its "<plugin>/<view>" id: centre a main view, or open the side panel on a panel view. The View
    /// menu's shortcuts and `wm.show`.
    @discardableResult
    func showView(_ id: String) -> Bool {
        if pluginView(id, place: "main") != nil {
            center = id
        } else if pluginView(id, place: "panel") != nil {
            panelTab = id
            if !showChanges { showChanges = true }
        } else {
            return false
        }
        return true
    }

    /// The View menu's items: each view with a `shortcut`, and the shortcut when it is one the app can use.
    var shortcutViews: [(view: PlacedView, shortcut: KeyboardShortcut?)] {
        var taken = Set(PluginShortcut.reserved)
        return (mainViews + panelViews).compactMap { placed in
            guard let text = placed.view.shortcut else { return nil }
            switch PluginShortcut.parse(text) {
            case .success(let parsed) where taken.insert(parsed.canonical).inserted:
                return (placed, parsed.keyboardShortcut)
            case .success:
                PluginShortcut.warn(placed.id, text, "it is taken by the app or another view")
            case .failure(let error):
                PluginShortcut.warn(placed.id, text, error.message)
            }
            return (placed, nil)
        }
    }

    // MARK: - From the server

    /// The plugins and their views. An older wm has no `plugins` request: then there are none. After
    /// `plugins_changed` the pages load again, since their files may have changed with the plugin's code.
    func loadPlugins() async {
        do {
            let result: PluginsResult = try await backend.call("plugins")
            if result.plugins != plugins {
                let pageFocused = (terminal.view.window?.firstResponder as? NSView).map { view in
                    sequence(first: view, next: \.superview).contains { $0 is PluginContainerView }
                } ?? false
                let wasCentred = pluginCentred
                plugins = result.plugins
                // the centred page that had the focus is gone: typing goes to the terminal again, not nowhere
                if pageFocused && wasCentred && center == "terminal" { terminal.focus() }
                syncPluginViews()  // the centred view may be gone, and the chips' repo with it
            }
            if pluginsDir != result.dir { pluginsDir = result.dir }
            if reloadPages {
                reloadPages = false
                for host in pluginHosts.allObjects { host.reload() }
            }
        } catch let error as BackendError where error.stopped {
            // restarting: the next start lists them
        } catch let error as BackendError where error.message.contains("unknown method 'plugins'") {
            if !plugins.isEmpty { plugins = [] }  // a wm from before plugins
            pluginsDir = nil
        } catch {
            NSLog("wm: couldn't list the plugins: %@", error.localizedDescription)
        }
    }

    func pluginEvent(_ name: String, _ data: Data) {
        switch name {
        case "plugins_changed":
            // The server can't unload a plugin's old code: start a new one, which lists the plugins as they are now.
            // Requests the pages are waiting on fail with "wm restarted; try again"; the pages load again once the new
            // server has listed the plugins.
            reloadPages = true
            restart()
        case "plugin_changed":
            guard let info = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let plugin = info["plugin"] as? String, let key = info["key"] as? String else { return }
            for host in pluginHosts.allObjects where host.plugin.name == plugin { host.watchedChanged(key: key) }
        default:
            break
        }
    }

    /// A state push: each page gets its workstream's row (and a new `wm.context` if that changed).
    func pluginStatePushed() {
        for host in pluginHosts.allObjects { host.sync(state: true) }
    }

    /// The selection or the repo picked in the top bar changed: tell the pages.
    func syncPluginViews() {
        for host in pluginHosts.allObjects { host.sync(state: false) }
    }

    // MARK: - What pages ask for (wm.openDiff, wm.panel, wm.show, wm.reveal, wm.send, wm.toast)

    /// The changes panel on Changes, narrowed to `repo` and scrolled to `path` (the panel follows `jump`). From a main
    /// view the panel opens beside it.
    func openDiff(key: String, repo: String, path: String, line: Int?) throws {
        guard key == selection, let ws = selected else { throw BackendError(message: "\(key) is no longer selected") }
        guard ws.repos.contains(where: { $0.repo == repo }) else {
            throw BackendError(message: "\(key) has no repo \(repo)")
        }
        soloRepos[key] = repo
        panelTab = "changes"
        if !showChanges { showChanges = true }
        jump = DiffJump(repo: repo, path: path, startLine: line)
    }

    /// Show or hide the side panel.
    func setPanel(_ open: Bool) {
        if showChanges != open { showChanges = open }
    }

    /// Switch to "terminal", "changes" or another view of `plugin` (by its id in the manifest).
    func show(_ viewID: String, of plugin: String) throws {
        switch viewID {
        case "terminal":
            center = "terminal"
        case "changes":
            panelTab = "changes"
            if !showChanges { showChanges = true }
        default:
            guard showView("\(plugin)/\(viewID)") else {
                throw BackendError(message: "plugin \(plugin) has no view '\(viewID)'")
            }
        }
    }

    /// Reveal a file in Finder: only one inside the workstream's folder (a relative path is taken from there).
    func reveal(_ path: String, in key: String) throws {
        guard let ws = (state.workstreams + state.archived).first(where: { $0.key == key }) else {
            throw BackendError(message: "no workstream \(key)")
        }
        let folder = URL(fileURLWithPath: ws.folder, isDirectory: true)
        let url = (path.hasPrefix("/") ? URL(fileURLWithPath: path) : URL(fileURLWithPath: path, relativeTo: folder))
            .standardizedFileURL
        guard FileManager.default.fileExists(atPath: url.path) else {
            throw BackendError(message: "no such file: \(path)")
        }
        // symlinks resolved on both sides: a link inside the folder that leads out of it is outside
        guard Self.contains(folder, url) else { throw BackendError(message: "outside the workstream") }
        reveal(url.path)
    }

    /// Whether `url` is `folder` or inside it, symlinks resolved.
    nonisolated static func contains(_ folder: URL, _ url: URL) -> Bool {
        let root = folder.standardizedFileURL.resolvingSymlinksInPath().path
        let path = url.standardizedFileURL.resolvingSymlinksInPath().path
        return path == root || path.hasPrefix(root.hasSuffix("/") ? root : root + "/")
    }

    /// Type `text` into the workstream's Claude session and press Enter. wm refuses when the session is stopped.
    func send(_ text: String, to key: String) async throws {
        _ = try await backend.requestData("send", ["key": key, "text": text])
    }

    /// Settings › Plugins: the plugins folder in Finder, made first if there is none yet.
    func revealPluginsFolder() {
        guard let dir = pluginsDir else { return }
        do {
            try FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        } catch {
            toast("Couldn't make \(dir): \(error.localizedDescription)", .error)
            return
        }
        reveal(dir)
    }

    /// Turn a plugin on or off. The server sees the plugins folder change, and the app restarts it.
    func setPlugin(_ name: String, on: Bool) async {
        do {
            _ = try await backend.requestData("set_plugin", ["name": name, "on": on])
        } catch {
            toast(error.localizedDescription, .error)
        }
    }
}

/// A view's `shortcut` from its manifest, such as "opt+cmd+m": modifiers and one key, joined by "+".
struct PluginShortcut: Equatable {
    struct Problem: Error { var message: String }

    var key: String  // a single lowercase character, or a named key ("return", "up", …)
    var modifiers: Set<String>  // "cmd", "opt", "ctrl", "shift"

    /// "ctrl+opt+shift+cmd+<key>", whatever order and spelling the manifest used: two views can't share one.
    var canonical: String {
        (["ctrl", "opt", "shift", "cmd"].filter(modifiers.contains) + [key]).joined(separator: "+")
    }

    private static let modifierNames = ["cmd": "cmd", "command": "cmd", "⌘": "cmd", "opt": "opt", "option": "opt",
                                        "alt": "opt", "⌥": "opt", "ctrl": "ctrl", "control": "ctrl", "⌃": "ctrl",
                                        "shift": "shift", "⇧": "shift"]
    private static let namedKeys: [String: KeyEquivalent] = [
        "return": .return, "escape": .escape, "space": .space, "tab": .tab, "delete": .delete, "up": .upArrow,
        "down": .downArrow, "left": .leftArrow, "right": .rightArrow, "home": .home, "end": .end, "pageup": .pageUp,
        "pagedown": .pageDown,
    ]
    private static let keyAliases = ["enter": "return", "esc": "escape", "backspace": "delete"]

    /// The app's own shortcuts and the standard ones, which a plugin can't take.
    static let reserved: [String] = {
        var keys = ["cmd+n", "cmd+j", "opt+cmd+up", "opt+cmd+down", "shift+cmd+return", "shift+cmd+a", "shift+cmd+b",
                    "cmd+delete", "cmd+r", "opt+cmd+0", "opt+cmd+a", "cmd+q", "cmd+w", "cmd+h", "opt+cmd+h", "cmd+m",
                    "cmd+,", "cmd+c", "cmd+v", "cmd+x", "cmd+a", "cmd+z", "shift+cmd+z", "ctrl+cmd+f", "ctrl+cmd+s",
                    "cmd+f", "cmd+g", "shift+cmd+g", "cmd+e"]
        keys += (0...9).map { "cmd+\($0)" }
        return keys
    }()

    static func parse(_ text: String) -> Result<PluginShortcut, Problem> {
        let parts = text.lowercased().split(separator: "+", omittingEmptySubsequences: false)
            .map { $0.trimmingCharacters(in: .whitespaces) }
        guard let last = parts.last, !last.isEmpty, !parts.dropLast().contains(where: \.isEmpty) else {
            return .failure(Problem(message: "write it as modifiers and a key joined by +, such as opt+cmd+m"))
        }
        var modifiers = Set<String>()
        for part in parts.dropLast() {
            guard let name = modifierNames[part] else { return .failure(Problem(message: "no modifier '\(part)'")) }
            modifiers.insert(name)
        }
        guard modifiers.contains("cmd") || modifiers.contains("ctrl") else {
            return .failure(Problem(message: "it needs cmd or ctrl, or it would take keys typed into the session"))
        }
        let key = keyAliases[last] ?? last
        guard key.count == 1 || namedKeys[key] != nil else { return .failure(Problem(message: "no key '\(key)'")) }
        return .success(PluginShortcut(key: key, modifiers: modifiers))
    }

    var keyboardShortcut: KeyboardShortcut {
        var flags: EventModifiers = []
        if modifiers.contains("cmd") { flags.insert(.command) }
        if modifiers.contains("opt") { flags.insert(.option) }
        if modifiers.contains("ctrl") { flags.insert(.control) }
        if modifiers.contains("shift") { flags.insert(.shift) }
        return KeyboardShortcut(Self.namedKeys[key] ?? KeyEquivalent(Character(key)), modifiers: flags)
    }

    @MainActor private static var warned: Set<String> = []

    /// Logged once per view and shortcut: the View menu shows the item without it.
    @MainActor static func warn(_ id: String, _ text: String, _ why: String) {
        guard warned.insert("\(id) \(text)").inserted else { return }
        NSLog("wm: plugin view %@: shortcut \"%@\" not used: %@", id, text, why)
    }
}
