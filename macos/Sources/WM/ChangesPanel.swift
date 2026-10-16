import SwiftUI

/// What the workstream's branches change against their base: commits, staged, unstaged and untracked files.
/// Each repo can be switched off with its chip at the top.
struct ChangesPanel: View {
    @Environment(AppModel.self) private var model
    @State private var expanded: Set<String> = []  // "repo/path" of big files opened by hand
    @State private var folded: Set<String> = []  // "repo/path" of files closed by hand

    private static let bigFile = 400

    var body: some View {
        Group {
            if let ws = model.selected, ws.status != .archived {
                if let diff = model.diff, diff.key == ws.key {
                    content(ws: ws, diff: diff)
                } else if ws.repos.isEmpty {
                    ContentUnavailableView("No repos yet", systemImage: "folder.badge.plus",
                                           description: Text("Changes show up here once repos are attached."))
                } else {
                    ProgressView().controlSize(.small)
                }
            } else if model.selected?.status == .archived {
                ContentUnavailableView("Worktrees removed", systemImage: "archivebox",
                                       description: Text("Restore the workstream to see its changes."))
            } else {
                ContentUnavailableView("No workstream selected", systemImage: "doc.text.magnifyingglass")
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func content(ws: Workstream, diff: DiffResult) -> some View {
        let shown = diff.repos.filter { model.repoShown(ws.key, $0.repo) }
        return VStack(spacing: 0) {
            header(ws: ws, diff: diff, shown: shown)
            Divider()
            if diff.repos.isEmpty {
                ContentUnavailableView("No repos yet", systemImage: "folder.badge.plus",
                                       description: Text("Changes show up here once repos are attached."))
                    .frame(maxHeight: .infinity)
            } else if shown.isEmpty {
                ContentUnavailableView("All repos hidden", systemImage: "eye.slash",
                                       description: Text("Turn a repo back on above."))
                    .frame(maxHeight: .infinity)
            } else if shown.allSatisfy({ $0.files.isEmpty && $0.error == nil }) {
                ContentUnavailableView("No changes yet", systemImage: "checkmark.circle",
                                       description: Text("The branches match their base."))
                    .frame(maxHeight: .infinity)
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 0, pinnedViews: [.sectionHeaders]) {
                            ForEach(shown) { repo in
                                Section {
                                    Color.clear.frame(height: 0).id(repo.repo)  // scroll target (the header is pinned)
                                    if let error = repo.error {
                                        Text(error).font(.caption).foregroundStyle(.red).padding(12)
                                    }
                                    ForEach(rows(repo)) { row in
                                        rowView(row)
                                    }
                                } header: {
                                    RepoHeader(repo: repo, state: ws.repos.first { $0.repo == repo.repo })
                                }
                            }
                        }
                    }
                    .onChange(of: model.focusRepo) { _, repo in
                        guard let repo else { return }
                        withAnimation(.snappy) { proxy.scrollTo(repo, anchor: .top) }
                    }
                }
            }
        }
    }

    private func header(ws: Workstream, diff: DiffResult, shown: [RepoDiff]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Changes").font(.headline)
                Spacer()
                let additions = shown.reduce(0) { $0 + $1.additions }, deletions = shown.reduce(0) { $0 + $1.deletions }
                if additions + deletions > 0 { LineCounts(additions: additions, deletions: deletions) }
            }
            if !diff.repos.isEmpty { chips(ws: ws, diff: diff) }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }

    private func chips(ws: Workstream, diff: DiffResult) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(diff.repos) { repo in
                    RepoChip(repo: repo, on: model.repoShown(ws.key, repo.repo)) {
                        withAnimation(.snappy) { model.toggleRepo(ws.key, repo.repo) }
                    }
                }
            }
        }
    }

    /// One flat list of rows per repo, each with its own id, so the lazy stack only builds what is on screen.
    private func rows(_ repo: RepoDiff) -> [Row] {
        var rows: [Row] = []
        for file in repo.files {
            let id = "\(repo.repo)/\(file.path)"
            let open = isOpen(id, file)
            rows.append(Row(id: id, kind: .file(file, open: open)))
            guard open else { continue }
            if file.binary { rows.append(Row(id: "\(id)#binary", kind: .note("Binary file"))) }
            for (h, hunk) in file.hunks.enumerated() {
                rows.append(Row(id: "\(id)#\(h)", kind: .hunk(hunk.header)))
                for (l, line) in hunk.lines.enumerated() {
                    rows.append(Row(id: "\(id)#\(h).\(l)", kind: .line(line)))
                }
            }
            if file.truncated {
                rows.append(Row(id: "\(id)#more", kind: .note(file.hunks.isEmpty ? "Too large to show"
                                                                                  : "More lines not shown")))
            }
        }
        return rows
    }

    private func isOpen(_ id: String, _ file: FileDiff) -> Bool {
        file.lineCount > Self.bigFile ? expanded.contains(id) : !folded.contains(id)
    }

    @ViewBuilder private func rowView(_ row: Row) -> some View {
        switch row.kind {
        case .file(let file, let open):
            FileHeader(file: file, open: open) {
                withAnimation(.snappy(duration: 0.2)) {
                    if file.lineCount > Self.bigFile {
                        if open { expanded.remove(row.id) } else { expanded.insert(row.id) }
                    } else {
                        if open { folded.insert(row.id) } else { folded.remove(row.id) }
                    }
                }
            }
        case .hunk(let header):
            HunkHeader(text: header)
        case .line(let line):
            DiffLineView(line: line)
        case .note(let text):
            Note(text: text)
        }
    }

    struct Row: Identifiable {
        enum Kind {
            case file(FileDiff, open: Bool)
            case hunk(String)
            case line(DiffLine)
            case note(String)
        }

        let id: String
        let kind: Kind
    }
}

private struct RepoChip: View {
    let repo: RepoDiff
    let on: Bool
    let toggle: () -> Void

    var body: some View {
        Button(action: toggle) {
            HStack(spacing: 5) {
                Image(systemName: on ? "checkmark.circle.fill" : "circle")
                    .foregroundStyle(on ? Color.accentColor : Color.secondary)
                Text(repo.repo).foregroundStyle(on ? .primary : .secondary)
                if repo.additions + repo.deletions > 0 {
                    LineCounts(additions: repo.additions, deletions: repo.deletions).opacity(on ? 1 : 0.5)
                }
            }
            .font(.caption)
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(on ? Color.accentColor.opacity(0.12) : Color.secondary.opacity(0.08), in: Capsule())
        }
        .buttonStyle(.plain)
        .help(on ? "Hide \(repo.repo)'s changes" : "Show \(repo.repo)'s changes")
    }
}

private struct RepoHeader: View {
    @Environment(AppModel.self) private var model
    let repo: RepoDiff
    let state: RepoState?

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "arrow.triangle.branch").font(.caption).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 1) {
                Text(repo.repo).font(.callout.weight(.semibold))
                Text("\(repo.branch) → \(repo.base)")
                    .font(.caption2.monospaced())
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer(minLength: 6)
            if let state {
                if let url = state.prUrl {
                    let number = state.pr.split(separator: " ").dropFirst().first.map(String.init)  // "PR #412 open"
                    Button(number ?? "PR") { model.open(url) }
                        .buttonStyle(.link)
                        .font(.caption)
                        .help("Open \(state.pr)")
                }
                Text(state.state).font(.caption).foregroundStyle(Color.tone(state.tone)).lineLimit(1)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .background(.bar)
        .overlay(alignment: .bottom) { Divider() }
    }
}

private struct FileHeader: View {
    let file: FileDiff
    let open: Bool
    let toggle: () -> Void

    var body: some View {
        Button(action: toggle) {
            HStack(spacing: 6) {
                Image(systemName: "chevron.right")
                    .font(.system(size: 9, weight: .semibold))
                    .rotationEffect(.degrees(open ? 90 : 0))
                    .foregroundStyle(.tertiary)
                    .frame(width: 10)
                Text(badge.letter)
                    .font(.system(size: 10, weight: .bold, design: .monospaced))
                    .foregroundStyle(badge.color)
                    .frame(width: 12)
                pathText
                    .lineLimit(1)
                    .truncationMode(.head)
                Spacer(minLength: 6)
                if !file.binary { LineCounts(additions: file.additions, deletions: file.deletions) }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .background(Color.secondary.opacity(0.06))
        .help(help)
    }

    private var pathText: Text {
        let url = URL(fileURLWithPath: file.path)
        let dir = url.deletingLastPathComponent().relativePath
        let name = Text(url.lastPathComponent).font(.caption.monospaced().weight(.semibold))
        guard dir != "." else { return name }
        return Text(dir + "/").font(.caption.monospaced()).foregroundColor(.secondary) + name
    }

    private var badge: (letter: String, color: Color) {
        if file.untracked { return ("U", .teal) }
        switch file.status {
        case "added": return ("A", .green)
        case "deleted": return ("D", .red)
        case "renamed": return ("R", .purple)
        default: return ("M", .orange)
        }
    }

    private var help: String {
        var lines = [file.oldPath.map { "\($0) → \(file.path)" } ?? file.path]
        if file.untracked { lines.append("untracked: not added to git yet") }
        return lines.joined(separator: "\n")
    }
}

private struct HunkHeader: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.system(size: 10.5, design: .monospaced))
            .foregroundStyle(.secondary)
            .lineLimit(1)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 12)
            .padding(.vertical, 3)
            .background(Color.blue.opacity(0.07))
    }
}

private struct DiffLineView: View {
    let line: DiffLine

    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            number(line.old)
            number(line.new)
            Text(line.kind == " " ? " " : line.kind)
                .foregroundStyle(tint.opacity(0.9))
                .frame(width: 14)
            Text(Self.keepIndent(line.text))
                .frame(maxWidth: .infinity, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
        }
        .font(.system(size: 11, design: .monospaced))
        .padding(.trailing, 8)
        .background(background)
    }

    /// Leading whitespace as no-break spaces, so a wrapped line keeps its indentation instead of breaking there.
    private static func keepIndent(_ text: String) -> String {
        let body = text.drop { $0 == " " || $0 == "\t" }
        let indent = text[..<body.startIndex].reduce(into: "") { out, c in
            out += c == "\t" ? String(repeating: "\u{00A0}", count: 4) : "\u{00A0}"
        }
        return text.isEmpty ? " " : indent + body
    }

    private func number(_ n: Int?) -> some View {
        Text(n.map(String.init) ?? "")
            .foregroundStyle(.tertiary)
            .frame(width: 36, alignment: .trailing)
            .padding(.trailing, 4)
    }

    private var tint: Color {
        switch line.kind {
        case "+": return .green
        case "-": return .red
        default: return .secondary
        }
    }

    private var background: Color {
        switch line.kind {
        case "+": return Color.green.opacity(0.12)
        case "-": return Color.red.opacity(0.12)
        default: return .clear
        }
    }
}

private struct Note: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.caption)
            .foregroundStyle(.secondary)
            .padding(.horizontal, 36)
            .padding(.vertical, 6)
    }
}
