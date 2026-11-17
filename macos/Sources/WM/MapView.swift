import SwiftUI
import WebKit

/// The task map in the changes panel: how it stands on one line (drawn when, up to date or not, a drawing in progress,
/// Redraw), the lens switch, and the map, or why there is none.
struct MapPanel: View {
    @Environment(AppModel.self) private var model
    @Environment(\.colorScheme) private var colorScheme
    let ws: Workstream
    let repo: String?  // the repo chip's pick, which narrows the map
    let changed: [String]  // workstream-relative paths in the current diff
    @State private var pageError: String?

    var body: some View {
        if let map = model.map, map.key == ws.key {
            content(map)
        } else {
            ProgressView().controlSize(.small).frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    @ViewBuilder private func content(_ map: MapResult) -> some View {
        if let doc = map.doc {
            VStack(spacing: 0) {
                statusLine(map)
                Divider()
                ZStack {
                    MapWebView(doc: doc, lens: shownLens(map), theme: colorScheme == .dark ? "dark" : "light",
                               repo: repo, changed: changed, restore: model.mapRestore,
                               onError: { error in pageError = error },
                               onOpen: { files, point, pan in model.openFromMap(files, at: point, pan: pan) },
                               onRestored: { model.mapRestore = nil })
                    if let pageError {
                        ContentUnavailableView {
                            Label("Can't draw this map", systemImage: "exclamationmark.triangle")
                        } description: {
                            Text(pageError)
                        } actions: {
                            redrawButton(map)
                        }
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .background(.background)
                    }
                }
            }
        } else if map.drawingSince != nil {
            ContentUnavailableView {
                Label("Drawing the map…", systemImage: "map")
            } description: {
                Text("Claude is drawing it in the background.")
            }
            .frame(maxHeight: .infinity)
        } else if let failure = map.failure {
            ContentUnavailableView {
                Label("The map couldn't be drawn", systemImage: "exclamationmark.triangle")
            } description: {
                Text(failure.reason)
            } actions: {
                HStack {
                    openLogButton
                    redrawButton(map)
                }
                .fixedSize()  // else the actions get squeezed and "Open log" truncates
            }
            .frame(maxHeight: .infinity)
        } else {
            ContentUnavailableView {
                Label("No map yet", systemImage: "map")
            } description: {
                Text("Drawn after the next commit.")
            } actions: {
                redrawButton(map)
            }
            .frame(maxHeight: .infinity)
        }
    }

    private func statusLine(_ map: MapResult) -> some View {
        HStack(spacing: 8) {
            Group {
                if map.drawingSince != nil {
                    HStack(spacing: 4) {
                        ProgressView().controlSize(.mini)
                        Text("Redrawing…")
                    }
                } else {
                    TimelineView(.periodic(from: .now, by: 30)) { context in
                        (Text(drawn(map, now: context.date)) + Text(" · ") + freshness(map))
                            .lineLimit(1)
                            .truncationMode(.tail)
                    }
                    .help(map.stale.map { "\($0.repo): \($0.reason)" }.joined(separator: "\n"))
                }
                if let failure = map.failure {  // an icon, so "Out of date" keeps its room in a narrow panel
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.red)
                        .help("The last drawing failed: " + failure.reason)
                    openLogButton.buttonStyle(.link).foregroundStyle(.tint)
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            Spacer(minLength: 6)
            if map.lenses.count > 1 {
                Picker("Lens", selection: Binding(get: { shownLens(map) }, set: { model.mapLenses[ws.key] = $0 })) {
                    ForEach(map.lenses, id: \.self) { Text(Self.lensNames[$0] ?? $0).tag($0) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .controlSize(.small)
                .fixedSize()
            }
            redrawButton(map).controlSize(.small)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
    }

    private static let lensNames = ["architecture": "Architecture", "data-flow": "Data flow", "data-model": "Data model"]

    private func shownLens(_ map: MapResult) -> String {
        if let lens = model.mapLenses[ws.key], map.lenses.contains(lens) { return lens }
        return map.lenses.first ?? "architecture"
    }

    private func drawn(_ map: MapResult, now: Date) -> String {
        guard let at = map.drawnAt else { return "Drawn" }
        let date = Date(timeIntervalSince1970: at)
        if now.timeIntervalSince(date) < 60 { return "Drawn just now" }
        return "Drawn " + Self.relative.localizedString(for: date, relativeTo: now)
    }

    private static let relative: RelativeDateTimeFormatter = {
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .short
        return f
    }()

    private func freshness(_ map: MapResult) -> Text {
        guard !map.stale.isEmpty else { return Text("Up to date") }
        let reasons = map.stale.map { "\($0.repo): \($0.reason)" }.joined(separator: " · ")
        return Text("Out of date").foregroundColor(.amber) + Text(" · " + reasons)
    }

    private func redrawButton(_ map: MapResult) -> some View {
        Button("Redraw") { Task { await model.redrawMap() } }
            .disabled(map.drawingSince != nil)
            .help(map.drawingSince != nil ? "A drawing is in progress" : "Draw the map again from the branches now")
    }

    private var openLogButton: some View {
        Button("Open log") { model.reveal(ws.folder + "/.wm/map/last-run.log") }
            .help("Show the last drawing's log in Finder")
    }
}

/// The bundled map page (`Resources/map`), drawing with PR Lens. `show` runs again whenever what it draws changes.
struct MapWebView: NSViewRepresentable {
    var doc: Data
    var lens: String
    var theme: String
    var repo: String?
    var changed: [String]
    var restore: MapPan?  // pan and zoom to put back once the map shows
    var onError: (String?) -> Void  // the page's error, nil once it draws
    var onOpen: ([MapFile], CGPoint, MapPan?) -> Void  // a card clicked: its changed files, the click (screen), the pan
    var onRestored: () -> Void  // `restore` is applied

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.userContentController.add(context.coordinator, name: "wm")
        let web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = context.coordinator
        web.underPageBackgroundColor = .clear
        context.coordinator.webView = web
        context.coordinator.load()
        return web
    }

    func updateNSView(_ web: WKWebView, context: Context) {
        context.coordinator.onError = onError
        context.coordinator.onOpen = onOpen
        // Hand the pan to put back over once: SwiftUI may update the view again before `onRestored` clears it, and
        // applying it again then would undo a pan made since.
        if restore != context.coordinator.handedRestore {
            context.coordinator.handedRestore = restore
            if let restore {
                context.coordinator.restore = restore
                DispatchQueue.main.async { onRestored() }  // not while SwiftUI is updating the view
            }
        }
        context.coordinator.update(Shown(doc: doc, lens: lens, theme: theme, repo: repo, changed: changed))
    }

    static func dismantleNSView(_ web: WKWebView, coordinator: Coordinator) {
        web.configuration.userContentController.removeScriptMessageHandler(forName: "wm")
    }

    struct Shown: Equatable {
        var doc: Data
        var lens: String
        var theme: String
        var repo: String?
        var changed: [String]
    }

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, WKScriptMessageHandler {
        weak var webView: WKWebView?
        var onError: ((String?) -> Void)?
        var onOpen: (([MapFile], CGPoint, MapPan?) -> Void)?
        var restore: MapPan?  // to put back with the next show, or now if the map shows already
        var handedRestore: MapPan?  // the `restore` last taken from the view, until the view's goes back to nil
        private var page: URL?
        private var loaded = false
        private var wanted: Shown?
        private var shown: Shown?

        func load() {
            guard let web = webView else { return }
            guard let url = Bundle.main.url(forResource: "index", withExtension: "html", subdirectory: "map") else {
                report("The map page is missing from the app.")
                return
            }
            page = url
            loaded = false
            shown = nil
            web.loadFileURL(url, allowingReadAccessTo: url.deletingLastPathComponent())
        }

        func update(_ next: Shown) {
            wanted = next
            if page == nil { report("The map page is missing from the app.") }
            push()
        }

        private func push() {
            guard loaded, let web = webView, let wanted else { return }
            let pan: Any = restore?.json ?? NSNull()
            restore = nil
            if wanted == shown {
                if !(pan is NSNull) {
                    web.callAsyncJavaScript("window.wmMap.restore(pan)", arguments: ["pan": pan], in: nil, in: .page)
                }
                return
            }
            shown = wanted
            let args: [String: Any] = ["doc": String(decoding: wanted.doc, as: UTF8.self), "lens": wanted.lens,
                                       "theme": wanted.theme, "repo": wanted.repo ?? NSNull(),
                                       "changed": wanted.changed, "pan": pan]
            web.callAsyncJavaScript("window.wmMap.show({doc, lens, theme, repo, changed}); window.wmMap.restore(pan)",
                                    arguments: args, in: nil, in: .page) { [weak self] result in
                if case .failure(let error) = result { self?.report(error.localizedDescription) }
            }
        }

        /// Later, never while SwiftUI is updating the view.
        private func report(_ error: String?) {
            DispatchQueue.main.async { [weak self] in self?.onError?(error) }
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            loaded = true
            push()
        }

        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
            load()  // WebKit's page process went away: start the page again, and show the map in it once it's up
        }

        /// The page and its scripts only: nothing else opens in here.
        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void) {
            let url = action.request.url
            decisionHandler(url?.isFileURL == true && url?.path == page?.path ? .allow : .cancel)
        }

        func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
            guard let body = message.body as? [String: Any], let type = body["type"] as? String else { return }
            switch type {
            case "error": report(body["message"] as? String ?? "The map page failed.")
            case "rendered": report(nil)
            case "open": open(body)
            default: break
            }
        }

        /// A card clicked: its changed files, and the click turned from the page's view into screen coordinates.
        private func open(_ body: [String: Any]) {
            guard let web = webView, let window = web.window, let raw = body["files"] as? [[String: Any]] else { return }
            let files = raw.compactMap { file -> MapFile? in
                guard let path = file["path"] as? String else { return nil }
                return MapFile(path: path, startLine: (file["startLine"] as? NSNumber)?.intValue)
            }
            guard !files.isEmpty else { return }
            let x = (body["x"] as? NSNumber)?.doubleValue ?? web.bounds.midX
            let y = (body["y"] as? NSNumber)?.doubleValue ?? web.bounds.midY
            let local = NSPoint(x: x, y: web.isFlipped ? y : web.bounds.height - y)
            let screen = window.convertPoint(toScreen: web.convert(local, to: nil))
            onOpen?(files, screen, MapPan(body["view"]))
        }
    }
}
