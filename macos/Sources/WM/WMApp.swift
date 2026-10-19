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
    func applicationDidFinishLaunching(_ notification: Notification) {
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
        CommandGroup(replacing: .help) {
            Button("Welcome Guide") { model.sheet = .guide(step: 0) }
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
        }
    }

    private func withSelected(archived: Bool = false, _ action: @escaping (String) async -> Void) {
        guard let ws = model.selected, (ws.status == .archived) == archived else { return }
        Task { await action(ws.key) }
    }
}

struct SettingsView: View {
    @Environment(AppModel.self) private var model
    @AppStorage("wmPath") private var wmPath = ""
    @AppStorage("optionAsMeta") private var optionAsMeta = false

    var body: some View {
        Form {
            TextField("wm executable", text: $wmPath, prompt: Text("found on your PATH"))
            Text("Leave empty to use the wm of the checkout the app was built from, else the one on your login "
                 + "shell's PATH. It needs a wm with the app server (`wm app-server`).")
                .font(.caption)
                .foregroundStyle(.secondary)
            HStack {
                Button("Repo Folders…") { model.sheet = .guide(step: 2) }
                    .help("Where wm finds your repos")
                Spacer()
                Button("Restart wm") { model.restart() }
            }
            Toggle("Use Option as Meta key in the terminal", isOn: $optionAsMeta)
                .onChange(of: optionAsMeta) { _, on in model.terminal.view.optionAsMetaKey = on }
            Text("On: Option+Enter and Option+letter shortcuts reach Claude. Off: Option types characters, as your "
                 + "keyboard layout defines them.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(20)
        .frame(width: 460)
    }
}
