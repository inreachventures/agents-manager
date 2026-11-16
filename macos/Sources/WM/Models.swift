import Foundation

// What `wm app-server` sends. Field names arrive in snake_case and are decoded with .convertFromSnakeCase.

struct DashboardState: Decodable, Equatable {
    var workstreams: [Workstream]
    var archived: [Workstream]
    var origin: Origin
    var clients: [String: String] = [:]  // tmux client pid -> the session it shows

    struct Origin: Decodable, Equatable {
        var repos: Int
        var checkedAt: Double?
    }

    static let empty = DashboardState(workstreams: [], archived: [], origin: Origin(repos: 0, checkedAt: nil))

    enum CodingKeys: String, CodingKey { case workstreams, archived, origin, clients }

    init(workstreams: [Workstream], archived: [Workstream], origin: Origin) {
        (self.workstreams, self.archived, self.origin) = (workstreams, archived, origin)
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        workstreams = try c.decode([Workstream].self, forKey: .workstreams)
        archived = try c.decode([Workstream].self, forKey: .archived)
        origin = try c.decode(Origin.self, forKey: .origin)
        clients = try c.decodeIfPresent([String: String].self, forKey: .clients) ?? [:]
    }
}

enum Status: String, Decodable {
    case needsYou = "needs_you", working, yourTurn = "your_turn", stopped, archived
    case unknown

    init(from decoder: Decoder) throws {
        self = Status(rawValue: try decoder.singleValueContainer().decode(String.self)) ?? .unknown
    }

    var isRunning: Bool { self == .needsYou || self == .working || self == .yourTurn }
}

struct Workstream: Decodable, Identifiable, Equatable {
    var key: String
    var ticket: String?
    var name: String?
    var label: String
    var status: Status
    var unseen: Bool?  // waiting on you and not looked at since; nil from an older wm: any waiting one counts
    var color: String?  // Claude's /color for the session (red, blue, …), nil for its default
    var statusText: String
    var since: Double
    var folder: String
    var intake: Bool
    var drift: String?
    var allMerged: Bool
    var baseCi: String?
    var readyToArchive: Bool?  // everything merged, CI passed or none on base (or the example)
    var openQuestions: Int?  // questions in the brief waiting on the user; nil from an older wm
    var waiting: Waiting?  // who it's waiting on: claude, job, you, done (stopped, archived); nil from an older wm
    var repos: [RepoState]

    /// Waiting on the user: the Dock badge and "next waiting" count these.
    var waitsOnYou: Bool { waiting.map { $0.on == "you" } ?? (status == .needsYou || status == .yourTurn) }

    var id: String { key }
    var ident: String { ticket ?? key }

    /// Waiting on you (your turn, or blocked on a question) and nobody has looked at the session since: what the dock
    /// badge counts and what pulses in the sidebar.
    var waitingUnseen: Bool { (status == .needsYou || status == .yourTurn) && unseen != false }

    /// What the sidebar and the title bar say: who it's waiting on, else wm's status text.
    var stateText: String {
        guard let waiting, status.isRunning || status == .unknown else { return statusText }
        return waiting.on == "you" && waiting.text != "your turn" ? "waiting on you: \(waiting.text)" : waiting.text
    }

    /// Show the archive button instead of the status dot: done, and Claude isn't busy or waiting on a question.
    var offersArchive: Bool { readyToArchive == true && (status == .yourTurn || status == .stopped) }

    /// The TUI's "merged → archive" hint, once every repo with work in it is merged. It starts with "merged", shown in
    /// purple as on GitHub; `tone` colours the rest: grey when there's nothing left but to archive.
    var mergeHint: (text: String, tone: String)? {
        // merged, but Claude or a job it started is still going: not a hint to archive yet
        guard allMerged, waiting?.on != "job", waiting?.on != "claude" else { return nil }
        switch baseCi {
        case "pass": return ("merged, CI passed · ready to archive", "grey")
        case "fail": return ("merged, CI failing on base", "red")
        case "pending": return ("merged, CI running on base…", "yellow")
        case "n/a": return ("merged, CI unknown", "yellow")
        default: return ("merged (no CI) · ready to archive", "grey")
        }
    }
}

struct Waiting: Decodable, Equatable {
    var on: String  // claude, job, you, done, stopped, archived
    var text: String
}

struct RepoState: Decodable, Identifiable, Equatable {
    var repo: String
    var branch: String
    var base: String
    var worktree: String
    var state: String  // what this repo needs next, e.g. "↑ needs push (2)"
    var tone: String
    var behind: Int
    var behindWarn: Bool
    var pr: String
    var prUrl: String?  // the PR that matters now: the open one, else the last merged
    var prs: [PRLink]?  // every PR `pr` lists, the workstream's PRs from its other branches too; nil from an older wm
    var merged: Bool
    var fetchError: String?

    var id: String { repo }

    struct PRLink: Decodable, Equatable, Identifiable {
        var number: Int
        var url: String?
        var label: String  // e.g. "PR #418 open ✓"
        var id: Int { number }
    }

    /// The PRs to link to: those `pr` lists, or just `prUrl`'s from an older wm.
    var prLinks: [PRLink] {
        if let prs, !prs.isEmpty { return prs }
        guard let prUrl, let number = pr.split(separator: " ").first(where: { $0.hasPrefix("#") })?.dropFirst(),
              let n = Int(number) else { return [] }
        return [PRLink(number: n, url: prUrl, label: pr)]
    }

    /// What its top-bar chip shows after the name: the number of the PR that matters now, else how far behind base
    /// once that matters.
    var chipExtra: String? {
        if let lead = prLinks.first(where: { $0.url == prUrl }) ?? prLinks.first { return "#\(lead.number)" }
        return behindWarn ? "\(behind)↓" : nil
    }

    /// Everything about it in a few lines, for tooltips.
    var details: String {
        var lines = ["\(repo) · \(branch) → \(base)", state, pr]
        if behind > 0 { lines.append("\(behind) behind base") }
        if let fetchError { lines.append("fetch failed: \(fetchError)") }
        return lines.joined(separator: "\n")
    }
}

// MARK: - Diffs

struct DiffResult: Decodable, Equatable {
    var key: String
    var repos: [RepoDiff]
    var digest: String?
    var unchanged: Bool?  // the server's diff matches `digest` sent with the request
}

struct RepoDiff: Decodable, Equatable, Identifiable {
    var repo: String
    var branch: String
    var base: String
    var forkPoint: String?
    var files: [FileDiff]
    var additions: Int
    var deletions: Int
    var error: String?
    var untrackedOmitted: Int?

    var id: String { repo }
}

struct FileDiff: Decodable, Equatable, Identifiable {
    var path: String
    var oldPath: String?
    var status: String
    var untracked: Bool
    var binary: Bool
    var additions: Int
    var deletions: Int
    var truncated: Bool
    var hunks: [Hunk]

    // a path can show twice: deleted from git and back as an untracked file (`git rm --cached`)
    var id: String { (untracked ? "untracked:" : "tracked:") + path }
    var lineCount: Int { hunks.reduce(0) { $0 + $1.lines.count } }
}

struct Hunk: Decodable, Equatable {
    var header: String
    var lines: [DiffLine]
}

struct DiffLine: Decodable, Equatable {
    var kind: String  // "+", "-" or " "
    var old: Int?
    var new: Int?
    var text: String
}

// MARK: - Brief (wm's ledger.py)

/// The Brief panel: questions waiting on the user, what Claude is doing, and the rules, facts and changes so far.
struct BriefResult: Decodable, Equatable {
    var key: String
    var digest: String?
    var unchanged: Bool
    var goal: String?
    var turn: Int
    var questions: [Question]
    var rules: [BriefItem]
    var facts: [BriefItem]
    var changes: [Change]
    var state: WorkState?
    var steps: [Step]
    var phases: [Phase]
    var jobs: [Job]
    var waiting: Waiting?
    var inbox: Int
    var threads: [SideThread]
    var activity: Activity?

    enum CodingKeys: String, CodingKey {
        case key, digest, unchanged, goal, turn, questions, rules, facts, changes, state, steps, phases, jobs, waiting,
             inbox, threads, activity
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = try c.decode(String.self, forKey: .key)
        digest = try c.decodeIfPresent(String.self, forKey: .digest)
        unchanged = try c.decodeIfPresent(Bool.self, forKey: .unchanged) ?? false
        goal = try c.decodeIfPresent(String.self, forKey: .goal)
        turn = try c.decodeIfPresent(Int.self, forKey: .turn) ?? 0
        questions = try c.decodeIfPresent([Question].self, forKey: .questions) ?? []
        rules = try c.decodeIfPresent([BriefItem].self, forKey: .rules) ?? []
        facts = try c.decodeIfPresent([BriefItem].self, forKey: .facts) ?? []
        changes = try c.decodeIfPresent([Change].self, forKey: .changes) ?? []
        state = try c.decodeIfPresent(WorkState.self, forKey: .state)
        steps = try c.decodeIfPresent([Step].self, forKey: .steps) ?? []
        phases = try c.decodeIfPresent([Phase].self, forKey: .phases) ?? []
        jobs = try c.decodeIfPresent([Job].self, forKey: .jobs) ?? []
        waiting = try c.decodeIfPresent(Waiting.self, forKey: .waiting)
        inbox = try c.decodeIfPresent(Int.self, forKey: .inbox) ?? 0
        threads = try c.decodeIfPresent([SideThread].self, forKey: .threads) ?? []
        activity = try c.decodeIfPresent(Activity.self, forKey: .activity)
    }

    /// Changed in the turn Claude just did (or by the user since): flagged "new" until the next message.
    func isRecent(_ itemTurn: Int?) -> Bool { turn > 0 && itemTurn == turn }

    func thread(for question: String) -> SideThread? { threads.first { $0.anchor.question == question } }
}

struct Question: Decodable, Equatable, Identifiable {
    var id: String
    var kind: String  // decision, assumption, blocker
    var title: String
    var context: String
    var options: [Option]
    var ifNoAnswer: String
    var revised: Bool?
    var turn: Int?

    struct Option: Decodable, Equatable {
        var label: String
        var effect: String
        var recommended: Bool?
    }

    var kindLabel: String {
        switch kind {
        case "assumption": return "Assumption to check"
        case "blocker": return "Only you can do this"
        default: return "Decision"
        }
    }
}

struct BriefItem: Decodable, Equatable, Identifiable {
    var id: String
    var text: String
    var source: String?  // facts: you, found, assumed
    var examples: [String]?
    var turn: Int?
    var by: String?

    var sourceLabel: String {
        switch source {
        case "you": return "you said"
        case "assumed": return "assumed"
        default: return "found"
        }
    }
}

struct Change: Decodable, Equatable {
    var text: String
    var turn: Int?
    var by: String?
    var at: Double?
}

/// Where the work stands, in Claude's words (`wm state`).
struct WorkState: Decodable, Equatable {
    var `where`: String?
    var at: Double?
}

/// A step of the work (`wm step`): who does it, and what it still waits on.
struct Step: Decodable, Equatable, Identifiable {
    var id: String
    var text: String
    var owner: String  // claude, you, job
    var status: String  // todo, doing, done
    var blockedBy: [Blocker]
    var doneAt: Double?
    var phase: String?

    struct Blocker: Decodable, Equatable {
        var id: String
        var text: String
    }
}

/// A stage of the plan (retrain → evaluate → decide → deploy). Owned by the user, it's a decision point, decided by a
/// question in Needs you.
struct Phase: Decodable, Equatable, Identifiable {
    var id: String
    var text: String
    var owner: String  // claude, you, job
    var status: String  // todo, doing, done, skipped
    var question: String?
    var current: Bool
    var questionOpen: Bool
    var decided: String?
}

/// A monitor, background command or background subagent Claude started and is waiting on.
struct Job: Decodable, Equatable {
    var id: String?
    var kind: String
    var text: String
    var startedAt: Double?
    var timeout: Double?
}

/// What happened since the user's last message, from the transcript: Claude's notes and each tool call with how it
/// ended (wm's transcript.Reader.activity).
struct Activity: Decodable, Equatable {
    var startedAt: Double?
    var events: [Event]

    struct Event: Decodable, Equatable {
        var kind: String  // tool, text
        var tool: String?
        var text: String
        var command: String?  // a shell command under its description
        var status: String  // running, done, failed, blocked
        var at: Double?
        var note: String?  // why it failed or was blocked
    }

    var running: Event? { events.last { $0.status == "running" } }
    var lastNote: Event? { events.last { $0.kind == "text" } }
}


struct SideThread: Decodable, Equatable, Identifiable {
    var id: String
    var anchor: Anchor
    var messages: [Message]
    var busy: Bool
    var error: String?

    struct Anchor: Decodable, Equatable {
        var question: String?
        var quote: String?
    }

    struct Message: Decodable, Equatable {
        var role: String
        var text: String
    }
}

struct OpenedThread: Decodable { var id: String }
struct AnswerResult: Decodable { var answered: [String]; var sent: Bool }
struct EditResult: Decodable { var note: String }
struct Empty: Decodable {}

// MARK: - Chat (wm's transcript.py)

struct TranscriptResult: Decodable, Equatable {
    var key: String
    var digest: String?
    var unchanged: Bool?
    var messages: [ChatMessage]
}

struct ChatMessage: Decodable, Equatable, Identifiable {
    var id: String
    var role: String  // user, claude, tools, wm, note
    var text: String
    var tools: [String]?
}

// MARK: - Action results

struct NewResult: Decodable { var key: String }
struct ExampleResult: Decodable { var key: String? }
struct LogResult: Decodable { var log: [String] }
struct AttachResult: Decodable { var argv: [String] }
struct SwitchResult: Decodable { var switched: Bool }
struct ColorResult: Decodable { var applied: Bool }
struct ReposResult: Decodable { var repos: [String] }
struct RefreshResult: Decodable { var failed: [String] }

struct AddRepoResult: Decodable {
    var repo: String
    var branch: String
    var createdBranch: Bool
    var notes: [String]
}

struct RebasePlan: Decodable, Identifiable {
    var label: String
    var steps: [Step]
    var id: String { label }

    struct Step: Decodable, Identifiable {
        var repo: String
        var branch: String
        var base: String
        var behind: Int
        var skip: String?
        var push: Bool
        var id: String { repo }
    }
}

struct ArchiveReport: Decodable, Identifiable {
    var label: String
    var repos: [Repo]
    var blocked: Bool
    var id: String { label }

    struct Repo: Decodable, Identifiable {
        var repo: String
        var branch: String
        var state: String
        var tone: String
        var pr: String
        var blockers: [String]
        var id: String { repo }
    }
}

struct Notice: Decodable {
    var level: String
    var message: String
}
