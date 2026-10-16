import Foundation

struct BackendError: LocalizedError {
    var message: String
    var errorDescription: String? { message }
}

/// Runs `wm app-server` and talks to it in JSON lines. The app never touches git, worktrees or sessions itself:
/// every action is a request to wm, which applies the same checks as the CLI and the TUI.
@MainActor
final class Backend {
    var onState: ((DashboardState) -> Void)?
    var onNotice: ((Notice) -> Void)?
    var onExit: ((String) -> Void)?

    private var process: Process?
    private var input: FileHandle?
    private var nextID = 1
    private var pending: [Int: CheckedContinuation<Any, Error>] = [:]
    private var buffer = Data()
    private var stderrTail = ""
    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    var isRunning: Bool { process?.isRunning ?? false }

    func start(wm: String, environment: [String: String]) throws {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: wm)
        proc.arguments = ["app-server"]
        proc.environment = environment
        proc.currentDirectoryURL = FileManager.default.homeDirectoryForCurrentUser
        let stdin = Pipe(), stdout = Pipe(), stderr = Pipe()
        // Our ends must not leak into processes the app forks (the terminal's tmux client): a leaked copy of the
        // write end would keep the server from ever seeing stdin close.
        for handle in [stdin.fileHandleForWriting, stdout.fileHandleForReading, stderr.fileHandleForReading] {
            _ = fcntl(handle.fileDescriptor, F_SETFD, FD_CLOEXEC)
        }
        proc.standardInput = stdin
        proc.standardOutput = stdout
        proc.standardError = stderr

        // DispatchQueue.main keeps the chunks in order (Tasks don't promise that)
        stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil }
            DispatchQueue.main.async { MainActor.assumeIsolated { self?.received(data, from: proc) } }
        }
        stderr.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil }
            guard let text = String(data: data, encoding: .utf8), !text.isEmpty else { return }
            FileHandle.standardError.write(data)
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.stderrTail = String((self.stderrTail + text).suffix(2000))
                }
            }
        }
        proc.terminationHandler = { [weak self] p in
            let status = p.terminationStatus
            DispatchQueue.main.async { MainActor.assumeIsolated { self?.exited(p, status) } }
        }
        try proc.run()
        process = proc
        input = stdin.fileHandleForWriting
    }

    /// Closing stdin lets the server finish what it is doing (an archive, say) and exit.
    func stop() {
        try? input?.close()
        input = nil
        if let proc = process {  // nothing more to wait for: report the exit now, so a restart can follow at once
            proc.terminationHandler = nil
            process = nil
            for (_, cont) in pending { cont.resume(throwing: BackendError(message: "wm stopped")) }
            pending.removeAll()
            buffer.removeAll()
        }
    }

    func call<T: Decodable>(_ method: String, _ params: [String: Any] = [:]) async throws -> T {
        let raw = try await send(method, params)
        let data = try JSONSerialization.data(withJSONObject: raw)
        return try decoder.decode(T.self, from: data)
    }

    private func send(_ method: String, _ params: [String: Any]) async throws -> Any {
        guard let input, isRunning else { throw BackendError(message: "wm is not running") }
        let id = nextID
        nextID += 1
        var line = try JSONSerialization.data(withJSONObject: ["id": id, "method": method, "params": params])
        line.append(0x0A)
        return try await withCheckedThrowingContinuation { cont in
            pending[id] = cont
            do {
                try input.write(contentsOf: line)
            } catch {
                pending[id] = nil
                cont.resume(throwing: error)
            }
        }
    }

    private func received(_ data: Data, from proc: Process) {
        guard proc === process else { return }  // the tail of a server already replaced
        buffer.append(data)
        while let nl = buffer.firstIndex(of: 0x0A) {
            let line = buffer[buffer.startIndex..<nl]
            buffer.removeSubrange(buffer.startIndex...nl)
            guard !line.isEmpty,
                  let obj = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] else { continue }
            if let event = obj["event"] as? String {
                handle(event: event, payload: obj["data"] ?? [:])
            } else if let id = obj["id"] as? Int, let cont = pending.removeValue(forKey: id) {
                if let error = obj["error"] as? String {
                    cont.resume(throwing: BackendError(message: error))
                } else {
                    cont.resume(returning: obj["result"] ?? [:])
                }
            }
        }
    }

    private func handle(event: String, payload: Any) {
        guard let data = try? JSONSerialization.data(withJSONObject: payload) else { return }
        switch event {
        case "state":
            if let state = try? decoder.decode(DashboardState.self, from: data) { onState?(state) }
        case "notice":
            if let notice = try? decoder.decode(Notice.self, from: data) { onNotice?(notice) }
        default:
            break
        }
    }

    private func exited(_ proc: Process, _ status: Int32) {
        guard proc === process else { return }  // an earlier server, already replaced
        process = nil
        input = nil
        for (_, cont) in pending { cont.resume(throwing: BackendError(message: "wm stopped")) }
        pending.removeAll()
        let tail = stderrTail.split(separator: "\n").suffix(3).joined(separator: "\n")
        onExit?(tail.isEmpty ? "wm app-server exited (status \(status))" : tail)
    }
}

/// A GUI app doesn't get your shell's PATH, so tmux, git, gh, claude and wm would not be found. Read the
/// environment from a login shell once, as terminal-based tools expect it.
enum ShellEnvironment {
    static func load() async -> [String: String] {
        await Task.detached(priority: .userInitiated) { () -> [String: String] in
            var env = ProcessInfo.processInfo.environment
            env.merge(fromLoginShell()) { _, shell in shell }
            let home = FileManager.default.homeDirectoryForCurrentUser.path
            var path = (env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin").split(separator: ":").map(String.init)
            for extra in ["/opt/homebrew/bin", "/usr/local/bin", "\(home)/.local/bin"] where !path.contains(extra) {
                path.append(extra)
            }
            env["PATH"] = path.joined(separator: ":")
            if env["LANG"] == nil { env["LANG"] = "en_US.UTF-8" }  // tmux draws unicode as "_" without it
            for key in ["TMUX", "TMUX_PANE", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "TERM_SESSION_ID"] {
                env[key] = nil
            }
            return env
        }.value
    }

    private static func fromLoginShell() -> [String: String] {
        let shell = ProcessInfo.processInfo.environment["SHELL"] ?? "/bin/zsh"
        let marker = "__WM_ENV__"
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: shell)
        proc.arguments = ["-l", "-i", "-c", "printf '\(marker)'; /usr/bin/env -0"]
        proc.standardInput = FileHandle.nullDevice
        let out = Pipe()
        proc.standardOutput = out
        proc.standardError = FileHandle.nullDevice
        do { try proc.run() } catch { return [:] }
        let deadline = DispatchTime.now() + 8
        DispatchQueue.global().asyncAfter(deadline: deadline) { if proc.isRunning { proc.terminate() } }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        guard let text = String(data: data, encoding: .utf8),
              let range = text.range(of: marker, options: .backwards) else { return [:] }
        var env: [String: String] = [:]
        for entry in text[range.upperBound...].split(separator: "\0") {
            guard let eq = entry.firstIndex(of: "=") else { continue }
            env[String(entry[..<eq])] = String(entry[entry.index(after: eq)...])
        }
        return env
    }

    /// `wm` to run: $WM_BIN, the path set in Settings, the checkout the app was built from (WMPath in
    /// Info.plist, set by build-app.sh), then the first `wm` on PATH.
    static func findWM(in env: [String: String]) -> String? {
        let candidates = [env["WM_BIN"], UserDefaults.standard.string(forKey: "wmPath"),
                          Bundle.main.object(forInfoDictionaryKey: "WMPath") as? String]
        let found = candidates.compactMap { $0 }.first { FileManager.default.isExecutableFile(atPath: $0) }
        return found ?? which("wm", in: env)
    }

    static func which(_ tool: String, in env: [String: String]) -> String? {
        for dir in (env["PATH"] ?? "").split(separator: ":") {
            let candidate = "\(dir)/\(tool)"
            if FileManager.default.isExecutableFile(atPath: candidate) { return candidate }
        }
        return nil
    }
}
