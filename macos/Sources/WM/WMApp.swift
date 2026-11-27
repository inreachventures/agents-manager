import AppKit
import SwiftUI

@main
struct WMApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @State private var model = AppModel.shared

    var body: some Scene {
        Window("Workstreams", id: "main") {
            ContentView()
                .environment(model)
                .frame(minWidth: 960, minHeight: 560)
                .task {
                    #if DEBUG
                    DebugControl.start(model: model)
                    #endif
                    await model.start()
                }
        }
        .defaultSize(width: 1440, height: 900)
        .windowToolbarStyle(.unified)
        .commands { WMCommands(model: model) }

        Settings {
            SettingsView().environment(model)
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationWillFinishLaunching(_ notification: Notification) {
        AppAppearance.saved.apply()  // before any window shows, so a light frame never flashes in Dark
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        Updater.shared.start()  // only when this build has a feed, so development builds stay silent
        NSApp.setActivationPolicy(.regular)  // also when run as a bare executable during development
        if ProcessInfo.processInfo.environment["WM_DEBUG_DIR"] == nil { NSApp.activate() }
    }

    // Sessions live in wm's tmux server, so closing the window loses nothing: `wm` or reopening the app brings
    // them back.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        MainActor.assumeIsolated { AppModel.shared.shutdown() }
    }
}

struct WMCommands: Commands {
    let model: AppModel

    var body: some Commands {
        CommandGroup(replacing: .newItem) {
            Button("New Workstream…") { model.sheet = .new }
                .keyboardShortcut("n")
        }
        CommandMenu("Workstream") {
            Button("Next Waiting on You") { model.selectNextWaiting() }
                .keyboardShortcut("j")
            Button("Previous Workstream") { model.selectAdjacent(-1) }
                .keyboardShortcut(.upArrow, modifiers: [.command, .option])
            Button("Next Workstream") { model.selectAdjacent(1) }
                .keyboardShortcut(.downArrow, modifiers: [.command, .option])
            Divider()
            Button("Resume") { withSelected { await model.resume($0) } }
                .keyboardShortcut(.return, modifiers: [.command, .shift])
            Button("Add Repo…") { withSelected { await model.startAddRepo($0) } }
                .keyboardShortcut("a", modifiers: [.command, .shift])
            Button("Rebase…") { withSelected { await model.startRebase($0) } }
                .keyboardShortcut("b", modifiers: [.command, .shift])
            Button("Archive…") { withSelected { await model.startArchive($0) } }
                .keyboardShortcut(.delete, modifiers: .command)
            Button("Restore") { withSelected(archived: true) { await model.restore($0) } }
            Divider()
            Button("Refresh Git, PR and CI State") { Task { await model.refresh() } }
                .keyboardShortcut("r")
            Button("Reveal in Finder") {
                if let ws = model.selected { model.reveal(ws.folder) }
            }
        }
        CommandGroup(after: .appInfo) {
            CheckForUpdatesView()
        }
        CommandGroup(replacing: .help) {
            Button("Show the Tour") { model.startTour() }
        }
        CommandGroup(after: .sidebar) {
            Button("Toggle Changes") { model.showChanges.toggle() }
                .keyboardShortcut("0", modifiers: [.command, .option])
            Button("Toggle Archived Workstreams") { model.showArchived.toggle() }
                .keyboardShortcut("a", modifiers: [.command, .option])
            Divider()
            Button("All Repos' Changes") { model.pickRepo(nil) }
                .keyboardShortcut("0")
            ForEach(0..<9, id: \.self) { index in
                Button(model.selected?.repos.indices.contains(index) == true
                       ? "\(model.selected!.repos[index].repo)'s Changes" : "Repo \(index + 1)'s Changes") {
                    model.pickRepo(at: index)
                }
                .keyboardShortcut(KeyEquivalent(Character("\(index + 1)")))
                .disabled(model.selected?.repos.indices.contains(index) != true)
            }
            Divider()
            // plugins' views that have a shortcut
            let active = model.selected.map { $0.status != .archived } ?? false
            ForEach(model.shortcutViews, id: \.view.id) { item in
                Button("Show \(item.view.view.title)") { model.showView(item.view.id) }
                    .keyboardShortcut(item.shortcut)
                    .disabled(!active)
            }
            if !model.shortcutViews.isEmpty { Divider() }
        }
    }

    private func withSelected(archived: Bool = false, _ action: @escaping (String) async -> Void) {
        guard let ws = model.selected, (ws.status == .archived) == archived else { return }
        Task { await action(ws.key) }
    }
}

/// App menu › Check for Updates…, in a View of its own as in Sparkle's SwiftUI setup: a `Commands` body may not
/// re-evaluate when the updater's state changes, a View's body does.
private struct CheckForUpdatesView: View {
    var body: some View {
        Button("Check for Updates…") { Updater.shared.checkForUpdates() }
            .disabled(!Updater.shared.canCheckForUpdates)
    }
}

struct SettingsView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("wmPath") private var wmPath = ""
    @AppStorage("optionAsMeta") private var optionAsMeta = false
    @AppStorage(AppAppearance.key) private var appearance = AppAppearance.system

    var body: some View {
        TabView {
            Form {
                RepoFoldersView()
            }
            .tabItem { Label("Repos", systemImage: "folder") }

            Form {
                Picker("Appearance", selection: $appearance) {
                    ForEach(AppAppearance.allCases) { Text($0.label).tag($0) }
                }
                .pickerStyle(.segmented)
                .onChange(of: appearance) { _, look in look.apply() }
                Text("Light or Dark keeps the app, its terminal included, in that look whatever macOS is set to. "
                     + "Claude's own colours are its theme: pick a matching one with /theme in a session.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .tabItem { Label("Appearance", systemImage: "circle.lefthalf.filled") }

            Form {
                Toggle("Use Option as Meta key", isOn: $optionAsMeta)
                    .onChange(of: optionAsMeta) { _, on in model.terminal.view.optionAsMetaKey = on }
                Text("On: Option+Enter and Option+letter shortcuts reach Claude. Off: Option types characters, as "
                     + "your keyboard layout defines them.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .tabItem { Label("Terminal", systemImage: "terminal") }

            Form {
                let bundled = ShellEnvironment.bundledWM
                TextField("wm executable", text: $wmPath,
                          prompt: Text(bundled == nil ? "found on your PATH" : "the one inside this app"))
                Text(bundled == nil
                     ? "Leave empty to use the wm of the checkout the app was built from, else the one on your "
                        + "login shell's PATH. It needs a wm with the app server (`wm app-server`)."
                     : "Leave empty to use the wm inside this app. Another wm needs the app server "
                        + "(`wm app-server`).")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if let bundled {
                    TerminalCommand(command: "sudo mkdir -p /usr/local/bin && sudo ln -sf "
                                    + "'\(bundled)' /usr/local/bin/wm")
                }
                HStack {
                    Spacer()
                    Button("Restart wm") { model.restart() }
                }
            }
            .tabItem { Label("wm", systemImage: "gearshape") }

            PluginsSettings()
                .tabItem { Label("Plugins", systemImage: "puzzlepiece.extension") }
        }
        .padding(20)
        .frame(width: 520)
    }
}

/// Settings › Plugins: each plugin in the plugins folder, on or off, and why one was skipped.
private struct PluginsSettings: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        Form {
            if model.plugins.isEmpty {
                Text("No plugins installed").foregroundStyle(.secondary)
            }
            ForEach(model.plugins) { plugin in
                HStack(alignment: .firstTextBaseline) {
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                            Text(plugin.title)
                            if !plugin.version.isEmpty {
                                Text(plugin.version).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        Text(status(plugin))
                            .font(.caption)
                            .foregroundStyle(plugin.status == "skipped" ? Color.orange : Color.secondary)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer()
                    let on = Binding(get: { plugin.status != "off" },
                                     set: { on in Task { await model.setPlugin(plugin.name, on: on) } })
                    Toggle(plugin.title, isOn: on)
                        .labelsHidden()
                        .toggleStyle(.switch)
                        .help(plugin.status == "off" ? "Turn \(plugin.title) on" : "Turn \(plugin.title) off")
                }
            }
            Text("A plugin is a folder in the plugins folder. wm restarts its server when one is added, changed or "
                 + "turned on or off.")
                .font(.caption)
                .foregroundStyle(.secondary)
            HStack {
                Spacer()
                Button("Reveal Plugins Folder in Finder") { model.revealPluginsFolder() }
                    .disabled(model.pluginsDir == nil)
            }
        }
    }

    private func status(_ plugin: PluginInfo) -> String {
        switch plugin.status {
        case "on":
            let views = plugin.views.map(\.title)
            return views.isEmpty ? "\(plugin.name): on" : "\(plugin.name): on, showing \(views.joined(separator: ", "))"
        case "off": return "\(plugin.name): off"
        default: return "\(plugin.name): skipped, \(plugin.reason ?? "for an unknown reason")"
        }
    }
}

/// Settings › Appearance: follow macOS, or keep the app light or dark whatever macOS is set to.
enum AppAppearance: String, CaseIterable, Identifiable {
    case system, light, dark

    static let key = "appearance"
    static var saved: AppAppearance {
        UserDefaults.standard.string(forKey: key).flatMap(AppAppearance.init(rawValue:)) ?? .system
    }

    var id: Self { self }
    var label: String { rawValue.capitalized }

    /// The terminal pane recolours itself when its effective appearance changes, so it follows too.
    @MainActor func apply() {
        switch self {
        case .system: NSApp.appearance = nil
        case .light: NSApp.appearance = NSAppearance(named: .aqua)
        case .dark: NSApp.appearance = NSAppearance(named: .darkAqua)
        }
    }
}

/// How to put the app's wm on PATH, to use it in Terminal as well.
private struct TerminalCommand: View {
    let command: String
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("To use wm in Terminal too:").font(.caption)
            HStack(alignment: .firstTextBaseline) {
                Text(command).font(.caption.monospaced()).textSelection(.enabled)
                Spacer()
                Button(copied ? "Copied" : "Copy") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(command, forType: .string)
                    copied = true
                }
            }
        }
    }
}
