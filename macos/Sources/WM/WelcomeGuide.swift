import AppKit
import SwiftUI

struct SetupInfo: Decodable {
    var codeRoots: [String]
    var repos: [String]
    var workstreamsDir: String
    var fromEnv: Bool
    var tools: [String: String?]
    var found: [String]
    var problems: [String]
}

struct ScanResult: Decodable {
    var found: [String]
    var problems: [String]
}

/// Three pages on first launch (and from Help): what a workstream is, how it spans repos, and where your repos are.
struct WelcomeGuide: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State var step = 0
    @State private var setup: SetupInfo?
    @State private var roots: [String] = []
    @State private var singles: [String] = []
    @State private var scan: ScanResult?
    @State private var saving = false
    @State private var error: String?

    var body: some View {
        VStack(spacing: 0) {
            Group {
                switch step {
                case 0: concept
                case 1: repos
                default: folders
                }
            }
            .frame(width: 560, height: 380, alignment: .topLeading)
            .padding(.horizontal, 32)
            .padding(.top, 30)
            Divider()
            HStack(spacing: 8) {
                HStack(spacing: 6) {
                    ForEach(0..<3) { i in
                        Circle().fill(i == step ? Color.accentColor : Color.secondary.opacity(0.3))
                            .frame(width: 7, height: 7)
                    }
                }
                .accessibilityLabel("Step \(step + 1) of 3")
                Spacer()
                if step > 0 {
                    Button("Back") { withAnimation(.snappy) { step -= 1 } }
                }
                Button(step < 2 ? "Next" : "Start", action: next)
                    .keyboardShortcut(.defaultAction)
                    .disabled(saving)
            }
            .padding(16)
        }
        .onExitCommand { finish() }
        .task { await load() }
    }

    // MARK: - Pages

    private var concept: some View {
        Page(symbol: "square.stack.3d.up", title: "One task, one workstream",
             lead: "A workstream is a task, often a ticket: one Claude conversation that keeps its brief and its "
                 + "repos together until the work is merged.") {
            Point(symbol: "plus.circle", text: "Press + and describe the task to Claude, by voice or text. "
                  + "Mention the ticket if there is one.")
            Point(symbol: "doc.text", text: "Claude names the workstream, writes the brief in TASK.md and attaches "
                  + "the repos it needs, then waits for your go.")
            Point(symbol: "circle.dotted", text: "In the sidebar a spinner means Claude is working, a pulsing dot "
                  + "means it's your turn: orange when it's blocked on a question or a permission.")
        }
    }

    private var repos: some View {
        Page(symbol: "arrow.triangle.branch", title: "Every repo the task touches",
             lead: "One conversation can change several repos at once, each on the task's own branch.") {
            Point(symbol: "folder.badge.plus", text: "Each repo gets its own git worktree inside "
                  + "\(short(setup?.workstreamsDir ?? "~/workstreams"))/<task>. Your checkouts stay as they are.")
            Point(symbol: "lock.shield", text: "Claude can't edit your checkouts, switch branches or make "
                  + "worktrees: wm blocks it and tells Claude what to do instead.")
            Point(symbol: "checklist", text: "The top bar shows what each repo needs next, from push to merge; "
                  + "click one to see its changes. Archive refuses while any work isn't pushed.")
        }
    }

    private var folders: some View {
        Page(symbol: "folder", title: "Where are your repos?",
             lead: "Pick the folder that holds your repos: wm finds every git repo directly inside it.") {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(Array(roots.enumerated()), id: \.offset) { index, root in
                    PathRow(symbol: "folder.fill", path: short(root)) {
                        Button("Change…") { pick(folder: true) { roots[index] = $0 } }
                        if roots.count > 1 {
                            Button { roots.remove(at: index); rescan() } label: { Image(systemName: "xmark") }
                                .buttonStyle(.borderless)
                                .help("Remove this folder")
                        }
                    }
                }
                if roots.isEmpty {
                    Button("Choose a Folder…") { pick(folder: true) { roots.append($0) } }
                }
                ForEach(Array(singles.enumerated()), id: \.offset) { index, repo in
                    PathRow(symbol: "shippingbox", path: short(repo)) {
                        Button { singles.remove(at: index); rescan() } label: { Image(systemName: "xmark") }
                            .buttonStyle(.borderless)
                            .help("Remove this repo")
                    }
                }
                Button {
                    pick(folder: false) { singles.append($0) }
                } label: {
                    Label("Add a repo from somewhere else…", systemImage: "plus")
                }
                .buttonStyle(.link)
                summary
            }
        }
    }

    @ViewBuilder private var summary: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let scan {
                let names = scan.found.prefix(6).joined(separator: ", ") + (scan.found.count > 6 ? ", …" : "")
                Text(scan.found.isEmpty ? "No repos found yet."
                     : "Found \(scan.found.count) repo\(scan.found.count == 1 ? "" : "s"): \(names)")
                    .foregroundStyle(.secondary)
                ForEach(scan.problems, id: \.self) { Text($0).foregroundStyle(.orange) }
            }
            ForEach(missingTools, id: \.self) { Text($0).foregroundStyle(.orange) }
            if setup?.fromEnv == true {
                Text("AGENTMGR_CODE_ROOTS is set, so it decides the folders while it is.").foregroundStyle(.secondary)
            }
            if let error { Text(error).foregroundStyle(.red) }
        }
        .font(.callout)
        .padding(.top, 4)
    }

    private var missingTools: [String] {
        guard let tools = setup?.tools else { return [] }
        let hints = [("tmux", "tmux isn't installed: brew install tmux"),
                     ("claude", "Claude Code isn't installed: see claude.com/claude-code"),
                     ("git", "git isn't installed: xcode-select --install")]
        return hints.compactMap { tool, hint in (tools[tool] ?? nil) == nil ? hint : nil }
    }

    // MARK: - Actions

    private func load() async {
        guard let info: SetupInfo = try? await model.backend.call("setup") else { return }
        setup = info
        roots = info.codeRoots
        singles = info.repos
        scan = ScanResult(found: info.found, problems: info.problems)
    }

    private func next() {
        if step < 2 {
            withAnimation(.snappy) { step += 1 }
            return
        }
        saving = true
        Task {
            defer { saving = false }
            do {
                let saved: SetupInfo = try await model.backend.call("save_setup", ["code_roots": roots, "repos": singles])
                model.toast("\(saved.found.count) repos ready. Press + to start a workstream.", .info)
                finish()
            } catch {
                self.error = error.localizedDescription
            }
        }
    }

    private func finish() {
        UserDefaults.standard.set(true, forKey: "guideSeen")
        dismiss()
    }

    private func rescan() {
        Task {
            scan = try? await model.backend.call("scan", ["code_roots": roots, "repos": singles])
        }
    }

    private func pick(folder: Bool, _ use: @escaping (String) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Choose"
        panel.message = folder ? "The folder that holds your repos" : "A git repo outside that folder"
        panel.directoryURL = URL(fileURLWithPath: roots.first.map { NSString(string: $0).expandingTildeInPath }
                                 ?? NSHomeDirectory())
        guard panel.runModal() == .OK, let url = panel.url else { return }
        use(url.path)
        error = nil
        rescan()
    }

    private func short(_ path: String) -> String {
        NSString(string: path).abbreviatingWithTildeInPath
    }
}

private struct Page<Content: View>: View {
    var symbol: String
    var title: String
    var lead: String
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Image(systemName: symbol)
                .font(.system(size: 30, weight: .light))
                .foregroundStyle(Color.accentColor)
            VStack(alignment: .leading, spacing: 6) {
                Text(title).font(.title2.weight(.semibold))
                Text(lead).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            VStack(alignment: .leading, spacing: 12) { content }
        }
    }
}

private struct Point: View {
    var symbol: String
    var text: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Image(systemName: symbol).foregroundStyle(.secondary).frame(width: 18)
            Text(text).fixedSize(horizontal: false, vertical: true)
        }
    }
}

private struct PathRow<Actions: View>: View {
    var symbol: String
    var path: String
    @ViewBuilder var actions: Actions

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: symbol).foregroundStyle(Color.accentColor)
            Text(path).font(.body.monospaced()).lineLimit(1).truncationMode(.middle)
            Spacer()
            actions
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .background(Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
    }
}
