import AppKit
import SwiftUI

/// The middle of the window: Claude's replies laid out as text (selectable, quotable), or the real terminal.
enum Center: String { case chat, terminal }

/// The right-hand panel's tabs. The first three read the brief (wm's ledger); Changes is the branches' diff.
enum Panel: String, CaseIterable, Identifiable {
    case status, brief, changes
    var id: String { rawValue }
    var title: String {
        switch self {
        case .status: return "Status"
        case .brief: return "Brief"
        case .changes: return "Changes"
        }
    }
}

/// An answer picked on a card in Needs you, held until every open question has one (or "Send now").
enum StagedAnswer: Equatable {
    case option(Int)
    case text(String)
}

struct ChatSelection: Equatable {
    var text: String
    var messageID: String?
}

extension AppModel {
    func stagedKey(_ q: Question) -> String { "\(selection ?? "")/\(q.id)" }

    // MARK: - Loading

    /// Whichever panel shows, every 2s while the app is in front; only what changed comes back.
    func loadPanel(polling: Bool = false) async {
        if panel == .changes { await loadDiff(polling: polling) } else { await loadBrief(polling: polling) }
    }

    func loadBrief(polling: Bool = false) async {
        guard showChanges, panel != .changes, !briefLoading, !polling || NSApp.isActive, let ws = selected,
              ws.status != .archived, phase == .ready else { return }
        briefLoading = true
        defer { briefLoading = false }
        var params: [String: Any] = ["key": ws.key]
        if let digest = brief?.digest, brief?.key == ws.key { params["since"] = digest }
        guard let result: BriefResult = try? await backend.call("brief", params), !result.unchanged else { return }
        if result.key == selected?.key {
            brief = result
            dropStale()
        }
    }

    func loadChat(polling: Bool = false) async {
        guard center == .chat, !chatLoading, !polling || NSApp.isActive, let ws = selected, ws.status != .archived,
              phase == .ready else { return }
        chatLoading = true
        defer { chatLoading = false }
        var params: [String: Any] = ["key": ws.key]
        if let digest = chat?.digest, chat?.key == ws.key { params["since"] = digest }
        guard let result: TranscriptResult = try? await backend.call("transcript", params), result.unchanged != true
        else { return }
        if result.key == selected?.key { chat = result }
    }

    /// Forget picks for questions that are gone (answered from another window, dropped in a discussion).
    private func dropStale() {
        guard let key = selection, let brief else { return }
        let open = Set(brief.questions.map { "\(key)/\($0.id)" })
        let gone = staged.keys.filter { $0.hasPrefix("\(key)/") && !open.contains($0) }
        for k in gone { staged[k] = nil }
        if sideThread != nil && !brief.threads.contains(where: { $0.id == sideThread }) { sideThread = nil }
        if !gone.isEmpty { scheduleAutoSend() }  // the rest may all be answered now
    }

    // MARK: - Answers

    /// Picking an answer folds the card; once every open question has one they go to Claude together.
    func stage(_ q: Question, _ answer: StagedAnswer) {
        let k = stagedKey(q)
        withAnimation(.snappy(duration: 0.2)) {
            staged[k] = answer
            reopened.remove(k)
            discussing.remove(k)
        }
        scheduleAutoSend()
    }

    func reopen(_ q: Question) {
        withAnimation(.snappy(duration: 0.2)) { _ = reopened.insert(stagedKey(q)) }
        autoSend?.cancel()
        sendingAnswers = false
    }

    var openQuestions: [Question] { brief?.key == selection ? brief?.questions ?? [] : [] }
    var stagedQuestions: [Question] { openQuestions.filter { staged[stagedKey($0)] != nil } }

    /// A short pause first, so the last pick shows before its card goes.
    func scheduleAutoSend() {
        autoSend?.cancel()
        let open = openQuestions
        guard !open.isEmpty, open.allSatisfy({ staged[stagedKey($0)] != nil }),
              !open.contains(where: { reopened.contains(stagedKey($0)) }) else {
            sendingAnswers = false
            return
        }
        sendingAnswers = true
        autoSend = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(1100))
            guard !Task.isCancelled else { return }
            await self?.sendAnswers(open)
        }
    }

    func sendStagedNow() async {
        autoSend?.cancel()
        await sendAnswers(stagedQuestions)
    }

    private func sendAnswers(_ questions: [Question]) async {
        guard let key = selection else { return }
        let picked = questions.compactMap { q -> [String: Any]? in
            switch staged[stagedKey(q)] {
            case .option(let i): return ["id": q.id, "option": i]
            case .text(let t): return ["id": q.id, "text": t]
            case nil: return nil
            }
        }
        guard !picked.isEmpty else { sendingAnswers = false; return }
        sendingAnswers = true
        defer { sendingAnswers = false }
        do {
            let result: AnswerResult = try await backend.call("brief_answer", ["key": key, "answers": picked])
            for q in questions { staged["\(key)/\(q.id)"] = nil }
            let n = result.answered.count
            let what = n == 1 ? "your answer" : "your \(n) answers"
            note(result.sent ? "Sent \(what). Claude is reading them now."
                             : "Sent \(what). Claude gets them at its next step, without a chat message.")
            await loadBrief()
        } catch {
            toast(error.localizedDescription, .error)
        }
    }

    private func note(_ text: String) {
        withAnimation(.snappy) { answersNote = text }
        Task {
            try? await Task.sleep(for: .seconds(6))
            if answersNote == text { withAnimation(.snappy) { answersNote = nil } }
        }
    }

    // MARK: - Editing the brief

    func editItem(_ kind: String, id: String?, text: String) async {
        guard let key = selection, !text.trimmingCharacters(in: .whitespaces).isEmpty else { return }
        var params: [String: Any] = ["key": key, "kind": kind, "text": text]
        if let id { params["id"] = id }
        await briefCall("brief_edit", params)
    }

    func removeItem(_ kind: String, id: String) async {
        guard let key = selection else { return }
        await briefCall("brief_edit", ["key": key, "kind": kind, "id": id, "remove": true])
    }

    private func briefCall(_ method: String, _ params: [String: Any]) async {
        do {
            let _: EditResult = try await backend.call(method, params)
            await loadBrief()
        } catch {
            toast(error.localizedDescription, .error)
        }
    }

    func toggleStep(_ step: Step) async {
        guard let key = selection else { return }
        await briefCall("brief_step", ["key": key, "id": step.id, "done": step.status != "done"])
    }

    // MARK: - Side discussions

    func toggleDiscussion(_ q: Question) {
        let k = stagedKey(q)
        withAnimation(.snappy(duration: 0.2)) {
            if discussing.contains(k) { discussing.remove(k) } else { discussing.insert(k) }
        }
    }

    /// The first message about a question opens its discussion; Claude answers in a fork of the conversation.
    func discuss(_ q: Question, _ text: String) async {
        guard let key = selection else { return }
        do {
            let tid: String
            if let existing = brief?.thread(for: q.id) {
                tid = existing.id
            } else {
                tid = (try await backend.call("side_open", ["key": key, "question": q.id]) as OpenedThread).id
            }
            let _: Empty = try await backend.call("side_ask", ["key": key, "id": tid, "text": text])
            await loadBrief()
        } catch {
            toast(error.localizedDescription, .error)
        }
    }

    func askOnTheSide(_ selection: ChatSelection) async {
        guard let key = self.selection else { return }
        var params: [String: Any] = ["key": key, "quote": selection.text]
        if let id = selection.messageID { params["message"] = id }
        guard let opened: OpenedThread = try? await backend.call("side_open", params) else { return }
        chatSelection = nil
        sideThread = opened.id
        await loadBrief(polling: false)
    }

    func sideAsk(_ tid: String, _ text: String) async {
        guard let key = selection else { return }
        do {
            let _: Empty = try await backend.call("side_ask", ["key": key, "id": tid, "text": text])
            await loadBrief()
        } catch {
            toast(error.localizedDescription, .error)
        }
    }

    func closeSide(_ tid: String) async {
        guard let key = selection else { return }
        if sideThread == tid { sideThread = nil }
        let _: Empty? = try? await backend.call("side_close", ["key": key, "id": tid])
        await loadBrief()
    }

    // MARK: - Chat

    func reply(_ selection: ChatSelection) {
        quote = selection.text
        chatSelection = nil
    }

    /// Typed in the chat view; goes to Claude as if typed in the terminal, quoting what Reply picked.
    func send(_ text: String) async -> Bool {
        guard let key = selection else { return false }
        var message = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !message.isEmpty else { return false }
        if let quote {
            let short = quote.count > 400 ? String(quote.prefix(400)) + "…" : quote
            message = "About this part of your reply: \"\(short)\" — \(message)"
        }
        do {
            let _: Empty = try await backend.call("send", ["key": key, "text": message])
            quote = nil
            try? await Task.sleep(for: .milliseconds(800))  // the transcript has the message by now
            await loadChat()
            return true
        } catch {
            toast(error.localizedDescription, .error)
            return false
        }
    }
}
