import AppKit
import SwiftUI

enum ActiveSheet: Identifiable {
    case new
    case addRepo(key: String, repos: [String])
    case rebase(key: String, plan: RebasePlan)
    case archive(key: String, report: ArchiveReport)

    var id: String {
        switch self {
        case .new: return "new"
        case .addRepo(let key, _): return "add-\(key)"
        case .rebase(let key, _): return "rebase-\(key)"
        case .archive(let key, _): return "archive-\(key)"
        }
    }
}

struct Toast: Identifiable, Equatable {
    enum Level { case info, warning, error }
    let id = UUID()
    var text: String
    var level: Level
}

@MainActor
@Observable
final class AppModel {
    enum Phase: Equatable { case starting, ready, failed(String) }

    static let shared = AppModel()

    var phase: Phase = .starting
    var state = DashboardState.empty
    var selection: String? { didSet { if oldValue != selection { selectionChanged() } } }  // a workstream key
    var showArchived = false { didSet { if oldValue != showArchived { selection = nil } } }
    // Opened once the first state is in: presented at launch, the inspector would take its minimum width.
    var showChanges = false {
        didSet {
            guard phase == .ready else { return }
            UserDefaults.standard.set(showChanges, forKey: "showChanges")
            if showChanges { Task { await loadDiff() } }
        }
    }
    var soloRepos: [String: String] = [:]  // per workstream: the one repo the changes panel shows (none: all)
    var diff: DiffResult?
    var busy: String?
    var refreshing = false
    var toasts: [Toast] = []
    var sheet: ActiveSheet?
    var terminalFailed: String?  // key whose tmux client exited right after attaching

    @ObservationIgnored let backend = Backend()
    @ObservationIgnored let terminal = TerminalController()
    @ObservationIgnored private var env: [String: String] = [:]
    @ObservationIgnored private var attaching: String?
    @ObservationIgnored private var diffLoading = false
    @ObservationIgnored private var diffLoop: Task<Void, Never>?
    @ObservationIgnored private var restarts = 0
    @ObservationIgnored private var quitting = false
    @ObservationIgnored private var focusObservers: [NSObjectProtocol] = []

    // MARK: - Derived

    var workstreams: [Workstream] { showArchived ? state.archived : state.workstreams }

    var selected: Workstream? {
        guard let key = selection else { return nil }
        return state.workstreams.first { $0.key == key } ?? state.archived.first { $0.key == key }
    }

    var waitingCount: Int { state.workstreams.filter { $0.status == .needsYou || $0.status == .yourTurn }.count }

    /// The repo the changes panel shows on its own, if any, for the selected workstream.
    var soloRepo: String? {
        guard let key = selection, let repo = soloRepos[key], selected?.repos.contains(where: { $0.repo == repo }) == true
        else { return nil }
        return repo
    }

    /// A repo chip in the top bar: show that repo's changes alone, or all of them again if it already is.
    func pickRepo(_ repo: String?) {
        guard let key = selection else { return }
        if repo == nil || (showChanges && soloRepo == repo) {
            soloRepos[key] = nil
        } else {
            soloRepos[key] = repo
        }
        if !showChanges { showChanges = true }
    }

    /// ⌘1…⌘9: the selected workstream's Nth repo.
    func pickRepo(at index: Int) {
        guard let key = selection, let repos = selected?.repos, repos.indices.contains(index) else { return }
        soloRepos[key] = repos[index].repo
        if !showChanges { showChanges = true }
    }

    // MARK: - Lifecycle

    func start() async {
        phase = .starting
        if env.isEmpty { env = await ShellEnvironment.load() }
        guard let wm = ShellEnvironment.findWM(in: env) else {
            phase = .failed("Can't find `wm` on your PATH. Install it (see the README), or set its path in Settings.")
            return
        }
        var termEnv = env
        termEnv["TERM"] = "xterm-256color"
        terminal.environment = termEnv.map { "\($0.key)=\($0.value)" }
        terminal.onDetached = { [weak self] key in self?.terminalDetached(key) }

        backend.onState = { [weak self] in self?.apply($0) }
        backend.onNotice = { [weak self] in self?.toast($0.message, $0.level == "warning" ? .warning : .info) }
        backend.onExit = { [weak self] in self?.backendExited($0) }
        do {
            try backend.start(wm: wm, environment: env)
        } catch {
            phase = .failed("Couldn't start \(wm): \(error.localizedDescription)")
            return
        }
        observeFocus()
        diffLoop?.cancel()
        diffLoop = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(3))
                await self?.loadDiff(polling: true)
            }
        }
    }

    /// Start over, e.g. after pointing Settings at another wm. The old server finishes any request in flight.
    /// Hooks notify only when nobody is looking at a session; the app looks only while it is in front.
    private func observeFocus() {
        if focusObservers.isEmpty {
            for (name, active) in [(NSApplication.didBecomeActiveNotification, true),
                                   (NSApplication.didResignActiveNotification, false)] {
                focusObservers.append(NotificationCenter.default.addObserver(forName: name, object: nil,
                                                                             queue: .main) { [weak self] _ in
                    MainActor.assumeIsolated { self?.reportFocus(active) }
                })
            }
        }
        reportFocus(NSApp.isActive)
    }

    private func reportFocus(_ active: Bool) {
        Task { let _: [String: String]? = try? await backend.call("focus", ["active": active]) }
    }

    func restart() {
        backend.stop()
        restarts = 0
        Task { await start() }
    }

    func shutdown() {
        quitting = true
        terminal.detach()
        backend.stop()
    }

    private func backendExited(_ message: String) {
        guard !quitting else { return }
        if message.contains("No such command") {
            phase = .failed("This wm is older than the app and has no app server. Update it, or point Settings at a "
                            + "newer one.")
            return
        }
        restarts += 1
        if restarts > 3 {
            phase = .failed("wm stopped:\n\(message)")
            return
        }
        toast("wm restarted: \(message)", .warning)
        Task {
            try? await Task.sleep(for: .seconds(1))
            await start()
        }
    }

    private func apply(_ new: DashboardState) {
        let first = phase != .ready
        if new != state {
            withAnimation(.snappy(duration: 0.25)) { state = new }
        }
        phase = .ready
        restarts = 0
        NSApp.dockTile.badgeLabel = waitingCount > 0 ? "\(waitingCount)" : nil
        if let key = terminal.key, let shown = new.clients[String(terminal.pid)], shown != key + "_app" {
            terminal.forget()  // tmux moved our client (C-b s, C-b ( …): put it back on the selected workstream
        }
        if first {
            if selection == nil {
                let saved = UserDefaults.standard.string(forKey: "selected")
                let key = state.workstreams.first { $0.key == saved }?.key ?? state.workstreams.first?.key
                if let key { selection = key }
            }
            showChanges = UserDefaults.standard.object(forKey: "showChanges") as? Bool ?? true
        }
        syncTerminal()
    }

    func reloadState() async {
        if let fresh: DashboardState = try? await backend.call("state") { apply(fresh) }
    }

    // MARK: - Selection and terminal

    private func selectionChanged() {
        if let selection { UserDefaults.standard.set(selection, forKey: "selected") }
        diff = nil
        terminalFailed = nil
        Task { await loadDiff() }
        syncTerminal()
    }

    /// Keep the terminal on the selected workstream while its session runs; let go of it otherwise.
    func syncTerminal() {
        guard attaching == nil else { return }
        guard let ws = selected, ws.status.isRunning, !showArchived, terminalFailed != ws.key else {
            terminal.detach()
            return
        }
        guard terminal.key != ws.key else { return }
        let key = ws.key
        attaching = key
        Task {
            do {
                try await terminal.show(key, backend: backend)
                terminal.focus()
            } catch {
                toast(error.localizedDescription, .error)
                terminalFailed = key
            }
            attaching = nil
            if selected?.key != key { syncTerminal() }  // the selection moved on meanwhile
        }
    }

    private func terminalDetached(_ key: String) {
        if Date().timeIntervalSince(terminal.startedAt) < 2 {
            terminalFailed = key  // the client quit right away: don't retry in a loop
            return
        }
        Task {
            await reloadState()  // Claude exited or the workstream was archived: show that, not a dead terminal
            syncTerminal()  // still running (C-b d, say): attach again
        }
    }

    func retryTerminal() {
        terminalFailed = nil
        syncTerminal()
    }

    /// The workstream above (-1) or below (+1) the selected one in the sidebar.
    func selectAdjacent(_ step: Int) {
        let keys = workstreams.map(\.key)
        guard !keys.isEmpty else { return }
        let index = selection.flatMap { keys.firstIndex(of: $0) }.map { $0 + step } ?? 0
        selection = keys[min(max(index, 0), keys.count - 1)]
    }

    func selectNextWaiting() {
        let order: [Status: Int] = [.needsYou: 0, .yourTurn: 1]
        let next = state.workstreams
            .filter { order[$0.status] != nil && $0.key != selection }
            .sorted { (order[$0.status]!, $0.since) < (order[$1.status]!, $1.since) }
            .first
        if let next {
            showArchived = false
            selection = next.key
        } else {
            toast("Nothing needs you", .info)
        }
    }

    // MARK: - Changes panel

    /// `polling`: the periodic refresh, skipped while the app is in the background.
    func loadDiff(polling: Bool = false) async {
        guard showChanges, !diffLoading, !polling || NSApp.isActive, let ws = selected, ws.status != .archived,
              phase == .ready else { return }
        diffLoading = true
        defer { diffLoading = false }
        var params: [String: Any] = ["key": ws.key]
        if let digest = diff?.digest, diff?.key == ws.key { params["since"] = digest }
        guard let result: DiffResult = try? await backend.call("diff", params), result.unchanged != true else { return }
        if result.key == selected?.key { diff = result }
    }

    // MARK: - Actions (each one is a wm request; wm does the checks)

    /// A wm request behind the busy indicator; errors (wm's own refusals included) become a toast.
    private func request<T: Decodable>(_ message: String, _ method: String, _ params: [String: Any] = [:]) async -> T? {
        if let busy {
            toast("Still busy: \(busy)", .warning)
            return nil
        }
        busy = message
        defer { busy = nil }
        do {
            return try await backend.call(method, params)
        } catch {
            toast(error.localizedDescription, .error)
            return nil
        }
    }

    func create(ticket: String) async {
        let params: [String: Any] = ticket.isEmpty ? [:] : ["ticket": ticket]
        guard let result: NewResult = await request("Creating the workstream…", "new", params) else { return }
        showArchived = false
        await reloadState()
        selection = result.key
    }

    func startAddRepo(_ key: String) async {
        guard let result: ReposResult = await request("Listing repos…", "repos") else { return }
        let attached = Set(state.workstreams.first { $0.key == key }?.repos.map(\.repo) ?? [])
        sheet = .addRepo(key: key, repos: result.repos.filter { !attached.contains($0) })
    }

    func addRepo(_ key: String, repo: String) async {
        guard let res: AddRepoResult = await request("Attaching \(repo) to \(key): fetching and creating the worktree…",
                                                     "add_repo", ["key": key, "repo": repo]) else { return }
        let how = res.createdBranch ? "new branch" : "existing branch"
        toast((["Attached \(res.repo) on \(how) \(res.branch)"] + res.notes).joined(separator: "\n"), .info)
        await loadDiff()
    }

    func resume(_ key: String) async {
        guard let _: NewResult = await request("Resuming \(key)…", "resume", ["key": key]) else { return }
        terminalFailed = nil
        await reloadState()
    }

    func startRebase(_ key: String) async {
        guard let plan: RebasePlan = await request("Checking \(key): fetching origin…", "rebase_plan", ["key": key])
        else { return }
        if plan.steps.allSatisfy({ $0.skip != nil }) {
            let reasons = plan.steps.map { "\($0.repo): \($0.skip ?? "")" }.joined(separator: "\n")
            toast("Nothing to rebase\n" + (reasons.isEmpty ? "no repos attached" : reasons), .info)
            return
        }
        sheet = .rebase(key: key, plan: plan)
    }

    func rebase(_ key: String) async {
        guard let result: LogResult = await request("Rebasing \(key)…", "rebase", ["key": key]) else { return }
        let text = result.log.joined(separator: "\n")
        let trouble = ["conflicts", "failed", "not pushed"].contains { text.contains($0) }
        toast(text, trouble ? .warning : .info)
    }

    func startArchive(_ key: String) async {
        guard let report: ArchiveReport = await request("Checking \(key): fetching repos and PR status…",
                                                        "archive_report", ["key": key]) else { return }
        sheet = .archive(key: key, report: report)
    }

    func archive(_ key: String, force: Bool) async {
        guard let result: LogResult = await request("Archiving \(key): stopping the session, removing worktrees…",
                                                    "archive", ["key": key, "force": force]) else { return }
        toast(result.log.joined(separator: "\n"), .info)
        await reloadState()
    }

    func restore(_ key: String) async {
        guard let result: LogResult = await request("Restoring \(key): fetching and recreating worktrees…",
                                                    "unarchive", ["key": key]) else { return }
        toast(result.log.joined(separator: "\n"), .info)
        showArchived = false
        await reloadState()
        selection = key
    }

    func refresh() async {
        guard !refreshing else { return }
        refreshing = true
        defer { refreshing = false }
        do {
            let result: RefreshResult = try await backend.call("refresh")
            if !result.failed.isEmpty { toast("Could not fetch: \(result.failed.joined(separator: ", "))", .warning) }
        } catch {
            toast(error.localizedDescription, .error)
        }
        await loadDiff()
    }

    func reveal(_ path: String) {
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
    }

    func open(_ url: String) {
        if let url = URL(string: url) { NSWorkspace.shared.open(url) }
    }

    // MARK: - Toasts

    func toast(_ text: String, _ level: Toast.Level) {
        let toast = Toast(text: text, level: level)
        withAnimation(.snappy) { toasts.append(toast) }
        let seconds: Double = level == .error ? 10 : (text.contains("\n") ? 8 : 5)
        Task {
            try? await Task.sleep(for: .seconds(seconds))
            dismiss(toast)
        }
    }

    func dismiss(_ toast: Toast) {
        withAnimation(.snappy) { toasts.removeAll { $0.id == toast.id } }
    }
}
