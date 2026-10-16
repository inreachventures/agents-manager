import SwiftUI

struct ContentView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        NavigationSplitView {
            SidebarView()
                .navigationSplitViewColumnWidth(min: 250, ideal: 300, max: 440)
        } detail: {
            DetailView()
        }
        .sheet(item: $model.sheet) { sheet in
            switch sheet {
            case .new: NewWorkstreamSheet()
            case .addRepo(let key, let repos): AddRepoSheet(key: key, repos: repos)
            case .rebase(let key, let plan): RebaseSheet(key: key, plan: plan)
            case .archive(let key, let report): ArchiveSheet(key: key, report: report)
            }
        }
        .overlay(alignment: .top) {
            if let busy = model.busy {
                HStack(spacing: 10) {
                    ProgressView().controlSize(.small)
                    Text(busy).font(.callout)
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 9)
                .background(.regularMaterial, in: Capsule())
                .shadow(color: .black.opacity(0.15), radius: 8, y: 2)
                .padding(.top, 10)
                .transition(.move(edge: .top).combined(with: .opacity))
            }
        }
        .overlay(alignment: .bottomTrailing) {
            VStack(alignment: .trailing, spacing: 8) {
                ForEach(model.toasts) { toast in
                    ToastView(toast: toast).onTapGesture { model.dismiss(toast) }
                }
            }
            .padding(16)
        }
        .animation(.snappy, value: model.busy)
    }
}

struct DetailView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        content
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .navigationTitle(model.selected?.label ?? "Workstreams")
            .navigationSubtitle(subtitle)
            .toolbar {
                if let ws = model.selected, ws.status != .archived, !ws.repos.isEmpty {
                    ToolbarItem(placement: .automatic) { RepoChips(ws: ws) }
                }
                ToolbarItemGroup(placement: .primaryAction) {
                    if model.refreshing {
                        ProgressView().controlSize(.small)
                    }
                    if let ws = model.selected, ws.status != .archived {
                        Menu {
                            WorkstreamMenu(ws: ws)
                            Divider()
                            Button("Refresh Git, PR and CI State") { Task { await model.refresh() } }
                        } label: {
                            Label("Actions", systemImage: "ellipsis.circle")
                        }
                        .help("Add repo, rebase, archive…")
                    }
                    Button {
                        model.showChanges.toggle()
                    } label: {
                        Label("Changes", systemImage: "sidebar.trailing")
                    }
                    .help("Show or hide the changes (⌥⌘0)")
                }
            }
            .inspector(isPresented: $model.showChanges) {
                ChangesPanel()
                    .inspectorColumnWidth(min: 320, ideal: 520, max: 1100)
            }
    }

    private var subtitle: String {
        guard let ws = model.selected else { return "" }
        var parts = [ws.statusText]
        if let hint = ws.mergeHint { parts.append(hint.text) }
        return parts.joined(separator: " · ")
    }

    @ViewBuilder private var content: some View {
        switch model.phase {
        case .starting:
            ProgressView("Starting wm…")
        case .failed(let message):
            FailedView(message: message)
        case .ready:
            if let ws = model.selected {
                if ws.status == .archived {
                    Placeholder(symbol: "archivebox", title: "Archived",
                                text: "Restoring recreates the worktrees on the same branches; opening it then resumes the same conversation.") {
                        Button("Restore") { Task { await model.restore(ws.key) } }
                            .buttonStyle(.borderedProminent)
                    }
                } else if model.terminalFailed == ws.key {
                    Placeholder(symbol: "exclamationmark.triangle", title: "Couldn't show the session",
                                text: "The terminal closed as soon as it attached.") {
                        Button("Try Again") { model.retryTerminal() }
                    }
                } else if ws.status.isRunning {
                    TerminalHost(controller: model.terminal)
                        .background(Color(nsColor: WMTerminalView.background))
                } else {
                    Placeholder(symbol: "pause.circle", title: "Session stopped",
                                text: "Resume picks up the same Claude conversation, with fresh git and PR state.") {
                        Button("Resume") { Task { await model.resume(ws.key) } }
                            .buttonStyle(.borderedProminent)
                            .keyboardShortcut(.defaultAction)
                    }
                }
            } else if model.state.workstreams.isEmpty && !model.showArchived {
                Placeholder(symbol: "square.stack.3d.up", title: "No workstreams yet",
                            text: "A new workstream opens a Claude session: describe the task by voice or text, and Claude names it, writes TASK.md and attaches the repos.") {
                    Button("New Workstream") { model.sheet = .new }
                        .buttonStyle(.borderedProminent)
                }
            } else {
                Placeholder(symbol: "sidebar.leading", title: "Select a workstream", text: nil) { EmptyView() }
            }
        }
    }
}

struct Placeholder<Actions: View>: View {
    var symbol: String
    var title: String
    var text: String?
    @ViewBuilder var actions: Actions

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: symbol)
                .font(.system(size: 34, weight: .light))
                .foregroundStyle(.tertiary)
            Text(title).font(.title3.weight(.semibold))
            if let text {
                Text(text)
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: 380)
            }
            actions.padding(.top, 4)
        }
        .padding(40)
    }
}

struct FailedView: View {
    @Environment(AppModel.self) private var model
    var message: String

    var body: some View {
        Placeholder(symbol: "bolt.horizontal.circle", title: "wm isn't running", text: message) {
            HStack {
                SettingsLink { Text("Settings…") }
                Button("Try Again") { model.restart() }
                    .buttonStyle(.borderedProminent)
            }
        }
    }
}

struct ToastView: View {
    var toast: Toast

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: icon).foregroundStyle(color)
            Text(toast.text)
                .font(.callout)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
        .frame(maxWidth: 440, alignment: .leading)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(color.opacity(0.25)))
        .shadow(color: .black.opacity(0.12), radius: 8, y: 2)
        .transition(.move(edge: .trailing).combined(with: .opacity))
    }

    private var icon: String {
        switch toast.level {
        case .info: return "checkmark.circle.fill"
        case .warning: return "exclamationmark.triangle.fill"
        case .error: return "xmark.octagon.fill"
        }
    }

    private var color: Color {
        switch toast.level {
        case .info: return .green
        case .warning: return .orange
        case .error: return .red
        }
    }
}

/// The selected workstream's repos, each with a dot for what it needs next and its PR number (or how far behind base
/// it is). Clicking one shows only its changes; clicking it again shows every repo's.
struct RepoChips: View {
    @Environment(AppModel.self) private var model
    let ws: Workstream
    private static let shown = 4  // the rest go in a "+N" menu, so the top bar keeps room

    var body: some View {
        HStack(spacing: 2) {
            ForEach(Array(ws.repos.prefix(Self.shown).enumerated()), id: \.element.id) { index, repo in
                chip(repo, index: index)
            }
            if ws.repos.count > Self.shown {
                Menu("+\(ws.repos.count - Self.shown)") {
                    ForEach(ws.repos.dropFirst(Self.shown)) { repo in
                        Button(repo.repo) { model.pickRepo(repo.repo) }
                    }
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .padding(.horizontal, 6)
            }
        }
    }

    private func chip(_ repo: RepoState, index: Int) -> some View {
        let on = model.showChanges && model.soloRepo == repo.repo
        let action = on ? "Click to show every repo's changes" : "Click to show only its changes"
        return Button {
            withAnimation(.snappy(duration: 0.2)) { model.pickRepo(repo.repo) }
        } label: {
            HStack(spacing: 5) {
                Circle().fill(Color.tone(repo.tone)).frame(width: 7, height: 7)
                Text(repo.repo)
                if let extra = repo.chipExtra {
                    Text(extra).foregroundStyle(on ? Color.white.opacity(0.75) : Color.secondary)
                }
            }
            .lineLimit(1)
            .padding(.horizontal, 10)
            .frame(height: 24)
            .foregroundStyle(on ? Color.white : Color.primary)
            .background(on ? Color.accentColor : Color.clear, in: Capsule())
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .help("\(repo.details)\n\(action)" + (index < 9 ? " (⌘\(index + 1))" : ""))
        .contextMenu { RepoMenu(repo: repo) }
    }
}
