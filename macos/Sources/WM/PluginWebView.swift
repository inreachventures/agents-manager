import AppKit
import SwiftUI
import WebKit

/// A plugin's page (`file://<plugin folder>/<page>`) with `window.wm` (PluginSDK.swift) for talking to the app. Keep
/// one alive per (plugin, view, workstream) while it is selected, so switching tabs doesn't reload it: give each an
/// `.id` of all three, so a coordinator is never handed another plugin's view. `visible` false hides it, loaded.
///
/// The page can read files in its plugin's folder and navigate within it; links to the web open in the default
/// browser. If WebKit's page process dies the page loads again once; a second death within a minute shows a
/// placeholder with Reload.
struct PluginWebView: NSViewRepresentable {
    var plugin: PluginInfo
    var view: PluginViewSpec
    var visible: Bool
    var takesFocus: Bool  // a main view: typing goes to the page when it is shown, not to the terminal under it
    @Environment(AppModel.self) private var model

    func makeCoordinator() -> Coordinator { Coordinator(plugin: plugin, view: view) }

    func makeNSView(context: Context) -> PluginContainerView {
        let config = WKWebViewConfiguration()
        // WebKit holds its message handlers strongly: a weak hop keeps the page from holding on to the coordinator
        config.userContentController.add(WeakScriptHandler(context.coordinator), name: "wm")
        let web = PluginWKWebView(frame: .zero, configuration: config)
        web.isInspectable = true  // Safari's Web Inspector, for plugin authors
        let container = PluginContainerView(web: web)
        context.coordinator.attach(model: model, web: web, container: container)
        return container
    }

    func updateNSView(_ container: PluginContainerView, context: Context) {
        context.coordinator.update(plugin: plugin, view: view)
        let shown = visible && container.isHidden
        container.isHidden = !visible
        if takesFocus && (shown || (visible && context.coordinator.firstShow)) {
            context.coordinator.firstShow = false
            DispatchQueue.main.async { [weak container] in  // once it is in the window
                guard let container, !container.isHidden else { return }
                container.window?.makeFirstResponder(container.web)
            }
        }
    }

    static func dismantleNSView(_ container: PluginContainerView, coordinator: Coordinator) {
        coordinator.detach()
    }

    /// `wm.context` as the page sees it.
    struct PageContext: Equatable, Encodable {
        var plugin: String
        var view: String
        var key: String?
        var folder: String?
        var repos: [Repo]
        var repo: String?  // the repo the top bar's chips narrow to, nil for all
        var theme: String  // "light" or "dark"

        struct Repo: Equatable, Encodable {
            var repo: String
            var branch: String
            var base: String
            var worktree: String
        }

        enum CodingKeys: String, CodingKey { case plugin, view, key, folder, repos, repo, theme }

        func encode(to encoder: Encoder) throws {  // nil as null, not left out: the page sees `key: null`
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(plugin, forKey: .plugin)
            try c.encode(view, forKey: .view)
            try c.encode(key, forKey: .key)
            try c.encode(folder, forKey: .folder)
            try c.encode(repos, forKey: .repos)
            try c.encode(repo, forKey: .repo)
            try c.encode(theme, forKey: .theme)
        }
    }

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
        private(set) var plugin: PluginInfo
        private(set) var view: PluginViewSpec
        private weak var model: AppModel?
        private weak var web: PluginWKWebView?
        private weak var container: PluginContainerView?
        private var context: PageContext?
        private var generation = 0  // bumped as each page commits, so an answer never reaches a later page
        private var crashReload: Date?  // when the page last loaded again after its process died
        var firstShow = true  // not yet given the focus

        init(plugin: PluginInfo, view: PluginViewSpec) {
            self.plugin = plugin
            self.view = view
        }

        private var root: URL { URL(fileURLWithPath: plugin.path, isDirectory: true) }

        func attach(model: AppModel, web: PluginWKWebView, container: PluginContainerView) {
            self.model = model
            self.web = web
            self.container = container
            web.navigationDelegate = self
            web.uiDelegate = self
            web.onAppearanceChange = { [weak self] in self?.appearanceChanged() }
            model.pluginHosts.add(self)
            context = makeContext()
            installScript()
            load()
        }

        func detach() {
            model?.pluginHosts.remove(self)
            guard let web else { return }
            web.stopLoading()
            web.onAppearanceChange = nil
            web.configuration.userContentController.removeScriptMessageHandler(forName: "wm")
            web.configuration.userContentController.removeAllUserScripts()
        }

        /// The view or its plugin as listed again (after a restart, say): a different page loads afresh.
        func update(plugin: PluginInfo, view: PluginViewSpec) {
            let moved = plugin.path != self.plugin.path || view.page != self.view.page
            self.plugin = plugin
            self.view = view
            if moved {
                context = makeContext()
                installScript()
                load()
            }
        }

        // MARK: - Loading

        /// The plugin's files may have changed (`plugins_changed`): load the page afresh.
        func reload() {
            crashReload = nil
            context = makeContext()
            installScript()
            load()
        }

        private func load() {
            guard let web else { return }
            container?.hideFailure()
            let page = URL(fileURLWithPath: view.page)
            guard AppModel.contains(root, page) else {
                fail("The \(view.title) page is outside its plugin's folder.", text: view.page)
                return
            }
            web.loadFileURL(page, allowingReadAccessTo: root)
        }

        private func fail(_ title: String, text: String?) {
            container?.showFailure(title: title, text: text) { [weak self] in
                self?.crashReload = nil
                self?.load()
            }
        }

        /// The SDK, then this page's first context and theme, at document start: the page's own scripts see them.
        private func installScript() {
            guard let web, let context else { return }
            let controller = web.configuration.userContentController
            controller.removeAllUserScripts()
            let boot = PluginSDK.source
                + "\nwindow.__wm.setTheme(\(Self.json(themeVars())), \(Self.json(context.theme)));"
                + "\nwindow.__wm.setContext(\(Self.json(context)));\n"
            controller.addUserScript(WKUserScript(source: boot, injectionTime: .atDocumentStart,
                                                  forMainFrameOnly: true))
        }

        // MARK: - What the app tells the page

        /// The theme changes only in `appearanceChanged`, which tells the page.
        private func makeContext() -> PageContext {
            let ws = model?.selected
            return PageContext(plugin: plugin.name, view: view.id, key: ws?.key, folder: ws?.folder,
                               repos: ws?.repos.map { .init(repo: $0.repo, branch: $0.branch, base: $0.base,
                                                            worktree: $0.worktree) } ?? [],
                               repo: model?.contextRepo, theme: context?.theme ?? theme)
        }

        /// A new `wm.context` when it changed: "workstream" for another workstream, "repo" for another repo picked in
        /// the top bar. `state`: also hand the page its workstream's row from the latest state push.
        func sync(state: Bool) {
            let next = makeContext()
            if next != context {
                let old = context
                context = next
                installScript()  // a reload of the page starts from it
                let script = "window.__wm.setContext(JSON.parse(context));"
                if old?.key != next.key {
                    run(script + "window.__wm.emit('workstream', window.wm.context);", ["context": Self.json(next)])
                } else if old?.repo != next.repo {
                    run(script + "window.__wm.emit('repo', {repo: window.wm.context.repo});",
                        ["context": Self.json(next)])
                } else {
                    run(script, ["context": Self.json(next)])
                }
            }
            if state { pushState() }
        }

        private func pushState() {
            guard let key = context?.key, let row = model?.backend.rows[key] else { return }
            emit("state", String(decoding: row, as: UTF8.self))
        }

        /// A path this plugin watches changed for workstream `key` (the server's `plugin_changed`).
        func watchedChanged(key: String) {
            guard key == context?.key else { return }
            emit("changed", Self.json(["plugin": plugin.name, "key": key]))
        }

        private func appearanceChanged() {
            guard var next = context, next.theme != theme else { return }
            next.theme = theme
            context = next
            installScript()
            run("window.__wm.setTheme(JSON.parse(vars), null); window.__wm.setContext(JSON.parse(context));"
                + "window.__wm.emit('theme', {theme: window.wm.context.theme});",
                ["vars": Self.json(themeVars()), "context": Self.json(next)])
        }

        /// `data`: JSON text.
        private func emit(_ event: String, _ data: String) {
            run("window.__wm.emit(event, JSON.parse(data));", ["event": event, "data": data])
        }

        /// Runs in the page's own world, where the SDK is; does nothing on a page without it.
        private func run(_ body: String, _ arguments: [String: Any]) {
            web?.callAsyncJavaScript("if (window.__wm) { \(body) }", arguments: arguments, in: nil, in: .page,
                                     completionHandler: nil)
        }

        // MARK: - Theme

        private var theme: String {
            let appearance = web?.effectiveAppearance ?? NSApp.effectiveAppearance
            return appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? "dark" : "light"
        }

        /// The app's colours as CSS for the page's `:root`, resolved for the web view's light or dark appearance.
        private func themeVars() -> [String: String] {
            var vars = ["--wm-font": "-apple-system, BlinkMacSystemFont, \"Helvetica Neue\", sans-serif",
                        "--wm-mono": "ui-monospace, SFMono-Regular, Menlo, monospace"]
            let colors: [(String, NSColor)] = [
                ("--wm-bg", .textBackgroundColor), ("--wm-fg", .labelColor), ("--wm-secondary", .secondaryLabelColor),
                ("--wm-accent", .controlAccentColor), ("--wm-border", .separatorColor),
                ("--wm-red", .systemRed), ("--wm-yellow", .amber), ("--wm-green", .systemGreen),
                ("--wm-blue", .systemBlue), ("--wm-cyan", .systemTeal), ("--wm-grey", .secondaryLabelColor),
                ("--wm-orange", .systemOrange),
                ("--wm-purple", .systemPurple), ("--wm-amber", .amber),
            ]
            (web?.effectiveAppearance ?? NSApp.effectiveAppearance).performAsCurrentDrawingAppearance {
                for (name, color) in colors { vars[name] = Self.css(color) }
            }
            return vars
        }

        private static func css(_ color: NSColor) -> String {
            guard let c = color.usingColorSpace(.sRGB) else { return "transparent" }
            let rgb = [c.redComponent, c.greenComponent, c.blueComponent].map { String(Int(($0 * 255).rounded())) }
            return "rgba(\(rgb.joined(separator: ", ")), \(String(format: "%.3g", c.alphaComponent)))"
        }

        private static func json<T: Encodable>(_ value: T) -> String {
            (try? String(decoding: JSONEncoder().encode(value), as: UTF8.self)) ?? "null"
        }

        // MARK: - What the page asks for

        func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
            guard message.frameInfo.isMainFrame, let body = message.body as? [String: Any],
                  let id = (body["id"] as? NSNumber)?.intValue, let call = body["call"] as? String else { return }
            let args = (body["args"] as? String)
                .flatMap { try? JSONSerialization.jsonObject(with: Data($0.utf8)) } as? [String: Any] ?? [:]
            // what the page was when it asked: a call from a page that has since been replaced is dropped
            let page = generation, key = context?.key, name = plugin.name
            Task { [weak self] in
                guard let self, page == self.generation, name == self.plugin.name else { return }
                do {
                    let result = try await self.perform(call, args, plugin: name, key: key)
                    self.answer(page, "window.__wm.resolve(id, JSON.parse(value));",
                                ["id": id, "value": String(decoding: result, as: UTF8.self)])
                } catch {
                    let stopped = (error as? BackendError)?.stopped == true
                    self.answer(page, "window.__wm.reject(id, message);",
                                ["id": id, "message": stopped ? "wm restarted; try again" : error.localizedDescription])
                }
            }
        }

        private func answer(_ page: Int, _ script: String, _ arguments: [String: Any]) {
            guard page == generation else { return }  // that page is gone; ids start again on the new one
            run(script, arguments)
        }

        /// One `wm` call from a page of `plugin`, for the workstream that was selected when the page made it. The
        /// result as JSON.
        private func perform(_ call: String, _ args: [String: Any], plugin: String, key: String?) async throws -> Data {
            guard let model else { throw BackendError(message: "the app is closing") }
            func text(_ name: String) throws -> String {
                guard let value = args[name] as? String else { throw BackendError(message: "wm.\(call): no \(name)") }
                return value
            }
            func workstream() throws -> String {
                guard let key else { throw BackendError(message: "no workstream is selected") }
                return key
            }
            switch call {
            case "request":
                let method = try text("method")
                guard let params = args["params"] as? [String: Any] else {
                    throw BackendError(message: "wm.request: params must be an object")
                }
                return try await model.backend.requestData("plugin_request",
                                                           ["plugin": plugin, "method": method, "params": params])
            case "send":
                try await model.send(try text("text"), to: try workstream())
            case "openDiff":
                try model.openDiff(key: try workstream(), repo: try text("repo"), path: try text("path"),
                                   line: (args["line"] as? NSNumber)?.intValue)
            case "panel":
                model.setPanel(args["open"] as? Bool ?? true)
            case "reveal":
                try model.reveal(try text("path"), in: try workstream())
            case "show":
                try model.show(try text("view"), of: plugin)
            case "toast":
                let level: Toast.Level = switch args["level"] as? String {
                case "warning": .warning
                case "error": .error
                default: .info
                }
                model.toast(try text("message"), level)
            default:
                throw BackendError(message: "wm has no call '\(call)'")
            }
            return Data("null".utf8)
        }

        // MARK: - Navigation

        /// Files in the plugin's folder only. Links to the web open in the default browser; anything else is refused.
        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void) {
            guard let url = action.request.url else { return decisionHandler(.cancel) }
            let mainFrame = action.targetFrame?.isMainFrame ?? true  // nil: a new window
            switch url.scheme?.lowercased() {
            case "file":
                decisionHandler(AppModel.contains(root, url) ? .allow : .cancel)
            case "http", "https":
                if mainFrame { NSWorkspace.shared.open(url) }  // not an embedded frame loading on its own
                decisionHandler(.cancel)
            case "about":
                // frames a page makes for itself (about:blank, srcdoc) hold nothing from elsewhere
                decisionHandler(!mainFrame && ["about:blank", "about:srcdoc"].contains(url.absoluteString)
                                ? .allow : .cancel)
            default:
                decisionHandler(.cancel)
            }
        }

        /// window.open: a web address opens in the browser; nothing opens in the app.
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let url = action.request.url, ["http", "https"].contains(url.scheme?.lowercased() ?? "") {
                NSWorkspace.shared.open(url)
            }
            return nil
        }

        func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
            generation += 1
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            pushState()
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                     withError error: Error) {
            failed(error)
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            failed(error)
        }

        private func failed(_ error: Error) {
            let e = error as NSError
            // a navigation this coordinator refused, or one the page cut short with another
            if e.domain == NSURLErrorDomain && e.code == NSURLErrorCancelled { return }
            if e.domain == "WebKitErrorDomain" && e.code == 102 { return }  // frame load interrupted by the policy
            fail("The \(view.title) page couldn't load.", text: e.localizedDescription)
        }

        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
            if let last = crashReload, Date().timeIntervalSince(last) < 60 {
                fail("The \(view.title) page stopped working.", text: nil)
                return
            }
            crashReload = Date()
            load()
        }
    }
}

/// The web view, with a placeholder over it when its page can't be shown.
final class PluginContainerView: NSView {
    let web: PluginWKWebView
    private var placeholder: NSView?

    init(web: PluginWKWebView) {
        self.web = web
        super.init(frame: .zero)
        web.frame = bounds
        web.autoresizingMask = [.width, .height]
        addSubview(web)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    func showFailure(title: String, text: String?, reload: @escaping () -> Void) {
        hideFailure()
        let view = NSHostingView(rootView: Placeholder(symbol: "exclamationmark.triangle", title: title, text: text) {
            Button("Reload", action: reload)
        }.frame(maxWidth: .infinity, maxHeight: .infinity))
        view.frame = bounds
        view.autoresizingMask = [.width, .height]
        addSubview(view)
        placeholder = view
        web.isHidden = true
    }

    func hideFailure() {
        placeholder?.removeFromSuperview()
        placeholder = nil
        web.isHidden = false
    }
}

/// Tells its coordinator when light or dark mode changes, so the page's theme follows.
final class PluginWKWebView: WKWebView {
    var onAppearanceChange: (() -> Void)?

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        onAppearanceChange?()
    }
}

/// Passes the page's messages on without keeping their receiver alive.
private final class WeakScriptHandler: NSObject, WKScriptMessageHandler {
    private weak var target: WKScriptMessageHandler?

    init(_ target: WKScriptMessageHandler) { self.target = target }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        target?.userContentController(controller, didReceive: message)
    }
}
