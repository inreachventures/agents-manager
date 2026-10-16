import SwiftUI

/// Workstreams in the TUI's order (needs you, working, your turn, intake, stopped), each one opening onto its repos.
struct SidebarView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        List(selection: $model.selection) {
            if model.showArchived {
                Section("Archived") {
                    ForEach(model.state.archived) { ws in
                        WorkstreamRow(ws: ws)
                            .tag(SidebarItem.workstream(ws.key))
                            .contextMenu { ArchivedMenu(ws: ws) }
                    }
                }
            } else {
                ForEach(model.state.workstreams) { ws in
                    DisclosureGroup(isExpanded: model.isExpanded(ws.key)) {
                        ForEach(ws.repos) { repo in
                            RepoRow(repo: repo)
                                .tag(SidebarItem.repo(ws.key, repo.repo))
                                .contextMenu { RepoMenu(repo: repo) }
                        }
                        if ws.repos.isEmpty {
                            Text(ws.intake ? "no repos yet: Claude attaches them at intake" : "no repos")
                                .font(.caption).foregroundStyle(.tertiary)
                                .selectionDisabled()
                        }
                    } label: {
                        WorkstreamRow(ws: ws)
                            .tag(SidebarItem.workstream(ws.key))
                            .contextMenu { WorkstreamMenu(ws: ws) }
                    }
                }
            }
        }
        .listStyle(.sidebar)
        .id(model.showArchived)  // a fresh list per mode: rows moving between the two structures leave ghosts
        .overlay {
            if model.phase == .ready && model.workstreams.isEmpty {
                Text(model.showArchived ? "Nothing archived" : "No workstreams")
                    .foregroundStyle(.tertiary)
            }
        }
        .safeAreaInset(edge: .bottom, spacing: 0) { footer }
        .toolbar {
            ToolbarItem {
                Button { model.sheet = .new } label: { Label("New Workstream", systemImage: "plus") }
                    .help("New workstream (⌘N)")
            }
        }
    }

    private var footer: some View {
        HStack(spacing: 8) {
            Button {
                model.showArchived.toggle()
            } label: {
                Label(model.showArchived ? "Active" : "Archived",
                      systemImage: model.showArchived ? "tray.full" : "archivebox")
                    .labelStyle(.titleAndIcon)
            }
            .buttonStyle(.borderless)
            .help(model.showArchived ? "Back to active workstreams" : "Show archived workstreams")
            Spacer()
            if model.state.origin.repos > 0 {
                TimelineView(.periodic(from: .now, by: 15)) { context in
                    Text(model.state.origin.checkedAt.map { "origin checked \(ago($0, now: context.date)) ago" }
                        ?? "checking origin…")
                }
                .font(.caption2)
                .foregroundStyle(.tertiary)
                .help("Each repo's base branch is fetched every 5 minutes (⌘R: now)")
            }
        }
        .font(.callout)
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }
}

struct WorkstreamRow: View {
    let ws: Workstream

    var body: some View {
        HStack(alignment: .center, spacing: 8) {
            StatusIndicator(status: ws.status)
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 5) {
                    Text(ws.ident).fontWeight(.semibold)
                    if let name = ws.name { Text(name).foregroundStyle(.primary) }
                }
                .lineLimit(1)
                subtitle
                    .font(.caption)
                    .lineLimit(1)
            }
            Spacer(minLength: 4)
            TimelineView(.periodic(from: .now, by: 15)) { context in
                Text(ago(ws.since, now: context.date))
            }
            .font(.caption2.monospacedDigit())
            .foregroundStyle(.tertiary)
        }
        .padding(.vertical, 2)
        .help(([ws.label, ws.statusText] + (ws.drift.map { ["drift: \($0)"] } ?? [])).joined(separator: "\n"))
    }

    @ViewBuilder private var subtitle: some View {
        if let hint = ws.mergeHint, ws.status != .needsYou {
            Text(hint.text).foregroundStyle(Color.tone(hint.tone))
        } else {
            Text(ws.statusText).foregroundStyle(ws.status == .stopped ? Color.secondary : Color.status(ws.status))
        }
    }
}

struct RepoRow: View {
    let repo: RepoState

    var body: some View {
        HStack(spacing: 6) {
            Text(repo.repo)
                .font(.callout)
                .lineLimit(1)
            Spacer(minLength: 6)
            if repo.fetchError != nil {
                Image(systemName: "exclamationmark.triangle").font(.caption2).foregroundStyle(.red)
            }
            if repo.behindWarn {
                Text("\(repo.behind)↓").font(.caption2.monospacedDigit()).foregroundStyle(Color.amber)
            }
            Text(repo.state)
                .font(.caption)
                .foregroundStyle(Color.tone(repo.tone))
                .lineLimit(1)
        }
        .help(details)
    }

    private var details: String {
        var lines = ["\(repo.repo) · \(repo.branch) → \(repo.base)", repo.state, repo.pr]
        if repo.behind > 0 { lines.append("\(repo.behind) behind base") }
        if let error = repo.fetchError { lines.append("fetch failed: \(error)") }
        return lines.joined(separator: "\n")
    }
}

struct WorkstreamMenu: View {
    @Environment(AppModel.self) private var model
    let ws: Workstream

    var body: some View {
        if ws.status == .stopped {
            Button("Resume") { Task { await model.resume(ws.key) } }
        }
        Button("Add Repo…") { Task { await model.startAddRepo(ws.key) } }
        Button("Rebase…") { Task { await model.startRebase(ws.key) } }
        Divider()
        Button("Reveal in Finder") { model.reveal(ws.folder) }
        Divider()
        Button("Archive…") { Task { await model.startArchive(ws.key) } }
    }
}

struct RepoMenu: View {
    @Environment(AppModel.self) private var model
    let repo: RepoState

    var body: some View {
        if let url = repo.prUrl {
            Button("Open \(repo.pr.split(separator: " ").prefix(2).joined(separator: " "))") { model.open(url) }
        }
        Button("Reveal Worktree in Finder") { model.reveal(repo.worktree) }
        Button("Copy Branch Name") {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(repo.branch, forType: .string)
        }
    }
}

struct ArchivedMenu: View {
    @Environment(AppModel.self) private var model
    let ws: Workstream

    var body: some View {
        Button("Restore") { Task { await model.restore(ws.key) } }
        Button("Reveal in Finder") { model.reveal(ws.folder) }
    }
}
