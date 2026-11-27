import AppKit
import SwiftUI

/// Where `wm.openDiff` sends the changes panel: a file of `repo` (`path` relative to it), at `line` if set.
struct DiffJump: Equatable {
    var repo: String
    var path: String
    var line: Int?
}

/// Plugins: their views in the main area and the side panel, the events the server sends about them, and what their
/// pages ask of the app through `window.wm` (PluginWebView.swift).
extension AppModel {
    // MARK: - Views

    /// Every main view of every plugin that is on, in plugin order (only those have views).
    var mainViews: [(PluginInfo, PluginViewSpec)] { views(place: "main") }

    /// Every side-panel view of every plugin that is on.
    var panelViews: [(PluginInfo, PluginViewSpec)] { views(place: "panel") }

    private func views(place: String) -> [(PluginInfo, PluginViewSpec)] {
        plugins.flatMap { plugin in plugin.views.filter { $0.place == place }.map { (plugin, $0) } }
    }

    /// A view by its "<plugin>/<view>" id, if a plugin that is on has it there.
    func pluginView(_ id: String, place: String) -> (PluginInfo, PluginViewSpec)? {
        views(place: place).first { Self.viewID($0.0, $0.1) == id }
    }

    static func viewID(_ plugin: PluginInfo, _ view: PluginViewSpec) -> String { "\(plugin.name)/\(view.id)" }

    /// The main area: "terminal" or a main view's "<plugin>/<view>". The choice is kept for the app; Terminal shows
    /// while the plugin it names is gone (or not listed yet).
    var center: String {
        get { pluginView(centerChoice, place: "main") == nil ? "terminal" : centerChoice }
        set {
            centerChoice = newValue
            UserDefaults.standard.set(newValue, forKey: "center")
        }
    }

    /// The side panel: "changes" or a panel view's "<plugin>/<view>", kept the same way.
    var panelTab: String {
        get { pluginView(panelTabChoice, place: "panel") == nil ? "changes" : panelTabChoice }
        set {
            panelTabChoice = newValue
            UserDefaults.standard.set(newValue, forKey: "panelTab")
        }
    }

    // MARK: - From the server

    /// The plugins and their views. An older wm has no `plugins` request: then there are none.
    func loadPlugins() async {
        do {
            let result: PluginsResult = try await backend.call("plugins")
            if result.plugins != plugins { plugins = result.plugins }
        } catch let error as BackendError where error.stopped {
            // restarting: the next start lists them
        } catch {
            if !plugins.isEmpty { plugins = [] }
        }
    }

    func pluginEvent(_ name: String, _ data: Data) {
        switch name {
        case "plugins_changed":
            // The server can't unload a plugin's old code: start a new one, which lists the plugins as they are now.
            // Requests the pages are waiting on fail with "wm restarted; try again".
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
        jump = DiffJump(repo: repo, path: path, line: line)
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
            let id = "\(plugin)/\(viewID)"
            if pluginView(id, place: "main") != nil {
                center = id
            } else if pluginView(id, place: "panel") != nil {
                panelTab = id
                if !showChanges { showChanges = true }
            } else {
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

    /// Turn a plugin on or off. The server sees the plugins folder change, and the app restarts it.
    func setPlugin(_ name: String, on: Bool) async {
        do {
            _ = try await backend.requestData("set_plugin", ["name": name, "on": on])
        } catch {
            toast(error.localizedDescription, .error)
        }
    }
}
