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

    static let claudeHint = "Claude Code isn't installed: see claude.com/claude-code"

    /// Required tools that aren't on PATH, with how to get them. claude: the command sessions run.
    var missingTools: [String] {
        [("tmux", "tmux isn't installed: brew install tmux"),
         ("claude", Self.claudeHint),
         ("git", "git isn't installed: xcode-select --install")]
            .compactMap { tool, hint in (tools[tool] ?? nil) == nil ? hint : nil }
    }

    var claudeMissing: Bool { (tools["claude"] ?? nil) == nil }
}

private struct ScanResult: Decodable {
    var found: [String]
    var problems: [String]
}

/// Settings › Repos: the folder that holds your repos, and repos kept anywhere else. Saved as you change it.
struct RepoFoldersView: View {
    @Environment(AppModel.self) private var model
    @State private var roots: [String] = []
    @State private var singles: [String] = []
    @State private var scan: ScanResult?
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("wm finds every git repo directly inside these folders.").foregroundStyle(.secondary)
            ForEach(Array(roots.enumerated()), id: \.offset) { index, root in
                PathRow(symbol: "folder.fill", path: root) {
                    Button("Change…") { pick(folder: true) { roots[index] = $0 } }
                    if roots.count > 1 {
                        Button { roots.remove(at: index); save() } label: { Image(systemName: "xmark") }
                            .buttonStyle(.borderless)
                            .help("Remove this folder")
                    }
                }
            }
            if roots.isEmpty {
                Button("Choose a Folder…") { pick(folder: true) { roots.append($0) } }
            }
            ForEach(Array(singles.enumerated()), id: \.offset) { index, repo in
                PathRow(symbol: "shippingbox", path: repo) {
                    Button { singles.remove(at: index); save() } label: { Image(systemName: "xmark") }
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
            VStack(alignment: .leading, spacing: 3) {
                if let scan {
                    let names = scan.found.prefix(6).joined(separator: ", ") + (scan.found.count > 6 ? ", …" : "")
                    Text(scan.found.isEmpty ? "No repos found yet."
                         : "\(scan.found.count) repo\(scan.found.count == 1 ? "" : "s"): \(names)")
                        .foregroundStyle(.secondary)
                    ForEach(scan.problems, id: \.self) { Text($0).foregroundStyle(.orange) }
                }
                ForEach(model.setup?.missingTools ?? [], id: \.self) { Text($0).foregroundStyle(.orange) }
                if model.setup?.fromEnv == true {
                    Text("AGENTMGR_CODE_ROOTS is set, so it decides the folders while it is.")
                        .foregroundStyle(.secondary)
                }
                if let error { Text(error).foregroundStyle(.red) }
            }
            .font(.callout)
        }
        .task { await load() }
    }

    private func load() async {
        await model.loadSetup()
        guard let setup = model.setup else { return }
        roots = setup.codeRoots.map(short)
        singles = setup.repos.map(short)
        scan = ScanResult(found: setup.found, problems: setup.problems)
    }

    /// Check the choice, then save it if wm finds nothing wrong with it.
    private func save() {
        error = nil
        Task {
            guard let result: ScanResult = try? await model.backend.call("scan", ["code_roots": roots,
                                                                                "repos": singles]) else { return }
            scan = result
            guard result.problems.isEmpty else { return }
            do {
                let saved: SetupInfo = try await model.backend.call("save_setup", ["code_roots": roots,
                                                                                 "repos": singles])
                model.setup = saved
            } catch {
                self.error = error.localizedDescription
            }
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
        use(short(url.path))
        save()
    }

    private func short(_ path: String) -> String {
        NSString(string: path).abbreviatingWithTildeInPath
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
        .padding(.vertical, 6)
        .background(Color.secondary.opacity(0.08), in: RoundedRectangle(cornerRadius: 7))
    }
}
