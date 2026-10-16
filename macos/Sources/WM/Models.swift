import Foundation

// What `wm app-server` sends. Field names arrive in snake_case and are decoded with .convertFromSnakeCase.

struct DashboardState: Decodable, Equatable {
    var workstreams: [Workstream]
    var archived: [Workstream]
    var origin: Origin

    struct Origin: Decodable, Equatable {
        var repos: Int
        var checkedAt: Double?
    }

    static let empty = DashboardState(workstreams: [], archived: [], origin: Origin(repos: 0, checkedAt: nil))
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
    var statusText: String
    var since: Double
    var folder: String
    var intake: Bool
    var drift: String?
    var allMerged: Bool
    var baseCi: String?
    var repos: [RepoState]

    var id: String { key }
    var ident: String { ticket ?? key }

    /// The TUI's "merged → archive" hint, once every repo with work in it is merged.
    var mergeHint: (text: String, tone: String)? {
        guard allMerged else { return nil }
        switch baseCi {
        case "pass": return ("merged, CI green · ready to archive", "green")
        case "fail": return ("merged, CI failing on base", "red")
        case "pending": return ("merged, CI running on base…", "yellow")
        case "n/a": return ("merged, CI unknown", "yellow")
        default: return ("merged (no CI) · ready to archive", "purple")
        }
    }
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
    var prUrl: String?
    var merged: Bool
    var fetchError: String?

    var id: String { repo }
}

// MARK: - Diffs

struct DiffResult: Decodable, Equatable {
    var key: String
    var repos: [RepoDiff]
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

    var id: String { path }
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

// MARK: - Action results

struct NewResult: Decodable { var key: String }
struct LogResult: Decodable { var log: [String] }
struct AttachResult: Decodable { var argv: [String] }
struct SwitchResult: Decodable { var switched: Bool }
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
