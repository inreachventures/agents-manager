import SwiftUI

/// The right-hand panel: Status (who it's waiting on, where things stand, what's next), the Brief (goal, rules,
/// facts) and the branches' Changes.
struct SidePanel: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(spacing: 0) {
            PanelTabs()
            Divider()
            Group {
                switch model.panel {
                case .changes: ChangesPanel()
                case .status: BriefScroll { StatusTab() }
                case .brief: BriefScroll { BriefTab() }
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

private struct PanelTabs: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        HStack(spacing: 2) {
            ForEach(Panel.allCases) { tab in
                Button {
                    model.panel = tab
                } label: {
                    HStack(spacing: 5) {
                        Text(tab.title)
                        if tab == .status, let ws = model.selected, ws.waitsOnYou, ws.status.isRunning {
                            Circle().fill(Color.waiting("you")).frame(width: 7, height: 7)
                                .help("Waiting on you")
                        }
                    }
                    .font(.callout.weight(model.panel == tab ? .semibold : .regular))
                    .padding(.horizontal, 10)
                    .frame(height: 26)
                    .foregroundStyle(model.panel == tab ? Color.primary : Color.secondary)
                    .background(model.panel == tab ? Color.primary.opacity(0.08) : .clear,
                                in: RoundedRectangle(cornerRadius: 6))
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
    }
}

extension Color {
    /// Who a workstream waits on: Claude (working), a job, you, or nobody (done).
    static func waiting(_ on: String?) -> Color {
        switch on {
        case "claude": return .green
        case "job": return .teal  // purple is GitHub's "merged"
        case "you": return .orange
        default: return .secondary
        }
    }
}

/// Shared frame for the brief's tabs: a placeholder until the brief loads.
private struct BriefScroll<Content: View>: View {
    @Environment(AppModel.self) private var model
    @ViewBuilder var content: Content

    var body: some View {
        if let ws = model.selected, ws.status != .archived {
            if model.brief?.key == ws.key {
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) { content }
                        .padding(14)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            } else {
                ProgressView().controlSize(.small)
            }
        } else {
            ContentUnavailableView("No workstream selected", systemImage: "list.bullet.rectangle")
        }
    }
}

// MARK: - Status

/// What someone coming back to the chat needs first: who it's waiting on, where the work stands, what needs them,
/// and the steps ahead. What already happened is folded away under History.
private struct StatusTab: View {
    @Environment(AppModel.self) private var model
    @State private var showDone = false
    @State private var showHistory = false

    var body: some View {
        if let ws = model.selected, let b = model.brief {
            StateCard(ws: ws, brief: b)
            if !b.phases.isEmpty { PlanStrip(brief: b) }
            if let note = model.answersNote {
                Label(note, systemImage: "checkmark.circle.fill").font(.callout).foregroundStyle(.green)
            }
            NeedsYou(brief: b)
            if !b.jobs.isEmpty {
                Group_(title: "Running") {
                    ForEach(Array(b.jobs.enumerated()), id: \.offset) { _, job in JobRow(job: job) }
                }
            }
            let open = b.steps.filter { $0.status != "done" && $0.owner != "you" }
            let ready = open.filter { $0.blockedBy.isEmpty }.sorted { ($0.status == "doing" ? 0 : 1) < ($1.status == "doing" ? 0 : 1) }
            let blocked = open.filter { !$0.blockedBy.isEmpty }
            let done = b.steps.filter { $0.status == "done" }
            if !ready.isEmpty {
                Group_(title: "Next up") { ForEach(ready) { StepRow(step: $0) } }
            }
            if !blocked.isEmpty {
                Group_(title: "Waiting") { ForEach(blocked) { StepRow(step: $0) } }
            }
            if b.steps.isEmpty && b.state?.where == nil {
                Text("Claude hasn't set out the steps for this workstream yet. They show up here once it records them, at the end of its next turn.")
                    .font(.callout).foregroundStyle(.tertiary).fixedSize(horizontal: false, vertical: true)
            }
            if !done.isEmpty {
                Fold(title: "Done", count: done.count, open: $showDone) {
                    ForEach(done.reversed()) { StepRow(step: $0) }
                }
            }
            Fold(title: "History", count: b.changes.count + (b.activity?.events.count ?? 0), open: $showHistory) {
                if !b.changes.isEmpty {
                    Text("What changed").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    ForEach(Array(b.changes.enumerated()), id: \.offset) { _, c in
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            if let at = c.at {
                                Text(Date(timeIntervalSince1970: at), format: .dateTime.month(.abbreviated).day().hour().minute())
                                    .font(.caption2.monospacedDigit()).foregroundStyle(.tertiary)
                                    .frame(width: 92, alignment: .leading)
                            }
                            Text(c.text).font(.callout).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                        }
                    }
                }
                if let events = b.activity?.events, !events.isEmpty {
                    Text("Last turn, step by step").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                        .padding(.top, 6)
                    ForEach(Array(events.enumerated().reversed()), id: \.offset) { _, e in
                        ActivityRow(event: e, emphasized: false)
                    }
                }
            }
        }
    }
}

/// The plan: its phases left to right, the current one ringed, the user's decision points in orange with what was
/// decided. Clicking a phase lists its steps.
private struct PlanStrip: View {
    var brief: BriefResult
    @State private var picked: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("PLAN").font(.caption.monospaced().weight(.medium)).foregroundStyle(.secondary)
            HStack(alignment: .top, spacing: 0) {
                ForEach(Array(brief.phases.enumerated()), id: \.element.id) { index, phase in
                    PhaseCell(phase: phase, number: index + 1, first: index == 0,
                              last: index == brief.phases.count - 1, picked: picked == phase.id,
                              stepCount: brief.steps.filter { $0.phase == phase.id }.count)
                        .onTapGesture {
                            withAnimation(.snappy(duration: 0.2)) { picked = picked == phase.id ? nil : phase.id }
                        }
                }
            }
            if let id = picked, let phase = brief.phases.first(where: { $0.id == id }) {
                let steps = brief.steps.filter { $0.phase == id }
                VStack(alignment: .leading, spacing: 6) {
                    Text(phase.text).font(.callout.weight(.semibold))
                    if let decided = phase.decided {
                        Text("You decided: \(decided)").font(.callout).foregroundStyle(.secondary)
                    } else if phase.questionOpen {
                        Text("Waiting on your answer in Needs you.").font(.callout).foregroundStyle(Color.orange)
                    }
                    if steps.isEmpty {
                        Text("No steps recorded for this phase.").font(.callout).foregroundStyle(.tertiary)
                    } else {
                        ForEach(steps) { StepRow(step: $0) }
                    }
                }
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 8))
            }
        }
    }
}

private struct PhaseCell: View {
    var phase: Phase
    var number: Int
    var first: Bool
    var last: Bool
    var picked: Bool
    var stepCount: Int

    private var tint: Color {
        switch phase.status {
        case "done": return .green
        case "skipped": return .secondary
        default: return phase.current || phase.owner == "you" ? Color.waiting(OwnerTag.on(phase.owner)) : .secondary
        }
    }

    var body: some View {
        VStack(spacing: 5) {
            HStack(spacing: 0) {
                Rectangle().fill(first ? Color.clear : Color.secondary.opacity(0.3)).frame(height: 1)
                marker
                Rectangle().fill(last ? Color.clear : Color.secondary.opacity(0.3)).frame(height: 1)
            }
            Text(phase.text)
                .font(.caption.weight(phase.current ? .semibold : .regular))
                .foregroundStyle(phase.status == "todo" && !phase.current ? Color.secondary : Color.primary)
                .strikethrough(phase.status == "skipped")
                .multilineTextAlignment(.center)
                .lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)
            Text(caption).font(.caption2).foregroundStyle(tint).lineLimit(2).multilineTextAlignment(.center)
        }
        .padding(.horizontal, 2)
        .padding(.vertical, 4)
        .frame(maxWidth: .infinity)
        .background(picked ? Color.primary.opacity(0.06) : Color.clear, in: RoundedRectangle(cornerRadius: 6))
        .contentShape(Rectangle())
        .help(phase.text + (stepCount > 0 ? "\n\(stepCount) step\(stepCount == 1 ? "" : "s") · click to see them" : ""))
    }

    @ViewBuilder private var marker: some View {
        let size: CGFloat = 20
        ZStack {
            switch phase.status {
            case "done":
                Image(systemName: "checkmark.circle.fill").resizable().foregroundStyle(.green)
            case "skipped":
                Image(systemName: "minus.circle").resizable().foregroundStyle(.secondary)
            default:
                if phase.owner == "you" {
                    Image(systemName: phase.current ? "diamond.fill" : "diamond").resizable()
                        .foregroundStyle(Color.orange)
                } else {
                    Circle().strokeBorder(tint, lineWidth: phase.current ? 2 : 1)
                        .background(Circle().fill(phase.current ? tint.opacity(0.18) : Color.clear))
                    Text("\(number)").font(.caption2.weight(.semibold).monospacedDigit())
                        .foregroundStyle(phase.current ? tint : Color.secondary)
                }
            }
        }
        .frame(width: size, height: size)
    }

    private var caption: String {
        switch phase.status {
        case "done": return phase.decided.map { "→ \($0)" } ?? "done"
        case "skipped": return "skipped"
        case "doing": return phase.owner == "you" ? "your call" : "in progress"
        default:
            return phase.owner == "you" ? phase.decided.map { "→ \($0)" } ?? "you decide" : " "
        }
    }
}

/// Who it's waiting on, in colour; where the work stands; and the next step with whose it is.
private struct StateCard: View {
    @Environment(AppModel.self) private var model
    var ws: Workstream
    var brief: BriefResult

    var body: some View {
        let on = brief.waiting?.on ?? ws.waiting?.on
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top, spacing: 10) {
                Circle().fill(Color.waiting(on)).frame(width: 10, height: 10).padding(.top, 5)
                VStack(alignment: .leading, spacing: 3) {
                    Text(title(on)).font(.headline).foregroundStyle(Color.waiting(on))
                    TimelineView(.periodic(from: .now, by: 5)) { context in
                        Text(detail(on, now: context.date)).font(.callout).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 0)
            }
            .padding(12)
            .background(Color.waiting(on).opacity(0.10))
            VStack(alignment: .leading, spacing: 8) {
                if let whereText = brief.state?.where ?? (on == "claude" ? brief.activity?.lastNote?.text : nil) {
                    Line(label: "Where", text: whereText)
                }
                if let next = nextStep {
                    Line(label: "Next", text: next.text, owner: next.owner)
                }
            }
            .padding(12)
        }
        .background(.background, in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.primary.opacity(0.1)))
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }

    /// The first step nobody is waiting on; a step that is in progress comes first.
    private var nextStep: Step? {
        let open = brief.steps.filter { $0.status != "done" && $0.blockedBy.isEmpty }
        return open.first { $0.status == "doing" } ?? open.first
    }

    private func title(_ on: String?) -> String {
        switch on {
        case "claude": return "Claude is working"
        case "job": return "Waiting on a job"
        case "you": return "Waiting on you"
        case "done": return "Done"
        case "stopped": return "Stopped"
        default: return ws.statusText
        }
    }

    private func detail(_ on: String?, now: Date) -> String {
        switch on {
        case "claude":
            let running = brief.activity?.running?.text
            let took = elapsed(brief.activity?.startedAt, now)
            return [took.map { "for \($0)" }, running.map { "now: \($0)" }].compactMap { $0 }.joined(separator: " · ")
        case "job":
            guard let job = brief.jobs.first else { return brief.waiting?.text ?? "" }
            let took = elapsed(job.startedAt, now).map { ", running \($0)" } ?? ""
            return "\(job.text)\(took). Claude picks up when it reports back; you don't need to do anything."
        case "you":
            let text = brief.waiting?.text ?? ws.waiting?.text ?? ""
            return text == "your turn" ? "Claude has stopped and nothing is running. Reply in the chat to continue." : text
        case "done": return "All steps are done. Review the result, then archive the workstream."
        default: return ws.statusText
        }
    }

    private func elapsed(_ start: Double?, _ now: Date) -> String? {
        guard let start else { return nil }
        let s = max(0, Int(now.timeIntervalSince1970 - start))
        return s >= 3600 ? "\(s / 3600)h \(s % 3600 / 60)m" : s >= 60 ? "\(s / 60)m" : "\(s)s"
    }
}

private struct Line: View {
    var label: String
    var text: String
    var owner: String? = nil

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(label.uppercased()).font(.caption2.monospaced()).foregroundStyle(.tertiary).frame(width: 44, alignment: .leading)
            (Text(text) + Text(owner == "you" ? "  · yours" : "")
                .foregroundColor(Color.waiting(OwnerTag.on(owner ?? ""))))
                .font(.callout)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
        }
    }
}

enum OwnerTag {
    static func name(_ owner: String) -> String {
        switch owner {
        case "you": return "you"
        case "job": return "a job"
        default: return "Claude"
        }
    }

    static func on(_ owner: String) -> String { owner == "claude" ? "claude" : owner }
}

/// Questions for the user and the steps that are theirs, at the top of Status.
private struct NeedsYou: View {
    @Environment(AppModel.self) private var model
    var brief: BriefResult

    var body: some View {
        let questions = model.openQuestions
        let mine = brief.steps.filter { $0.owner == "you" && $0.status != "done" }
        if !questions.isEmpty || !mine.isEmpty {
            Group_(title: "Needs you", tint: .orange) {
                SendBar(questions: questions)
                ForEach(questions) { q in
                    if model.staged[model.stagedKey(q)] != nil && !model.reopened.contains(model.stagedKey(q)) {
                        AnsweredRow(question: q)
                    } else {
                        QuestionCard(question: q)
                    }
                }
                ForEach(mine) { StepRow(step: $0) }
            }
        }
    }
}

private struct Group_<Content: View>: View {
    var title: String
    var tint: Color = .secondary
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title.uppercased()).font(.caption.monospaced().weight(.medium)).foregroundStyle(tint)
            content
        }
    }
}

private struct Fold<Content: View>: View {
    var title: String
    var count: Int
    @Binding var open: Bool
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                withAnimation(.snappy(duration: 0.2)) { open.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "chevron.right").font(.caption2.weight(.bold))
                        .rotationEffect(.degrees(open ? 90 : 0)).foregroundStyle(.tertiary)
                    Text(title.uppercased()).font(.caption.monospaced().weight(.medium)).foregroundStyle(.secondary)
                    Text("\(count)").font(.caption2.monospacedDigit()).foregroundStyle(.tertiary)
                    Spacer()
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if open { content }
        }
        .padding(.top, 4)
        .overlay(alignment: .top) { Divider().offset(y: -6) }
    }
}

/// A step: what has to happen, whose it is, what it waits on. The user can tick off their own steps (and reopen any).
private struct StepRow: View {
    @Environment(AppModel.self) private var model
    var step: Step

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Button {
                Task { await model.toggleStep(step) }
            } label: {
                Image(systemName: step.status == "done" ? "checkmark.circle.fill"
                      : step.status == "doing" ? "circle.dotted" : "circle")
                    .foregroundStyle(step.status == "done" ? Color.green : Color.waiting(OwnerTag.on(step.owner)))
            }
            .buttonStyle(.plain)
            .disabled(step.owner != "you" && step.status != "done")
            .help(step.owner == "you" ? (step.status == "done" ? "Mark as not done" : "Mark as done")
                  : step.status == "done" ? "Reopen" : "Claude ticks this off")
            VStack(alignment: .leading, spacing: 3) {
                Text(step.text)
                    .font(.callout.weight(step.status == "doing" ? .semibold : .regular))
                    .foregroundStyle(step.status == "done" ? Color.secondary : Color.primary)
                    .strikethrough(step.status == "done", color: .secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
                if step.status != "done" && (step.status == "doing" || !step.blockedBy.isEmpty) {
                    HStack(spacing: 6) {
                        if step.status == "doing" {  // whose step it is shows in the section it sits in
                            Text("in progress").font(.caption).foregroundStyle(.secondary)
                        }
                        ForEach(Array(step.blockedBy.enumerated()), id: \.offset) { _, blocker in
                            Text("waits on \(blocker.id == "job" ? "a running job" : "\(blocker.id): \(blocker.text)")")
                                .font(.caption)
                                .lineLimit(1)
                                .padding(.horizontal, 6)
                                .background(Color.primary.opacity(0.06), in: Capsule())
                                .foregroundStyle(.secondary)
                                .help(blocker.text)
                        }
                    }
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, 2)
    }
}

private struct JobRow: View {
    var job: Job

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            ProgressView().controlSize(.mini)
            VStack(alignment: .leading, spacing: 2) {
                Text(job.text).font(.callout).fixedSize(horizontal: false, vertical: true)
                TimelineView(.periodic(from: .now, by: 30)) { context in
                    Text(caption(context.date)).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }

    private func caption(_ now: Date) -> String {
        let kind = ["monitor": "Monitor", "command": "Background command", "agent": "Background agent"][job.kind] ?? "Job"
        guard let start = job.startedAt else { return kind }
        return "\(kind) · started \(ago(start, now: now)) ago"
    }
}

private struct SendBar: View {
    @Environment(AppModel.self) private var model
    var questions: [Question]

    var body: some View {
        let picked = model.stagedQuestions.count
        Group {
            if model.sendingAnswers {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text(picked == questions.count ? "All answered. Sending them to Claude…" : "Sending…")
                }
            } else if picked > 0 {
                HStack {
                    Text("\(picked) of \(questions.count) answered. The rest can wait.")
                    Spacer()
                    Button("Send \(picked) now") { Task { await model.sendStagedNow() } }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.small)
                }
            } else if questions.count > 1 {
                Text("Pick an answer on each. They go to Claude together once all are answered, without a chat message.")
                    .foregroundStyle(.secondary)
            }
        }
        .font(.callout)
    }
}

/// An answered card, folded to one line until it's sent; Change opens it again.
private struct AnsweredRow: View {
    @Environment(AppModel.self) private var model
    var question: Question

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "checkmark").font(.callout.weight(.bold)).foregroundStyle(.green)
            VStack(alignment: .leading, spacing: 1) {
                Text(question.title).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                Text(answer).font(.callout.weight(.medium))
            }
            Spacer(minLength: 4)
            Button("Change") { model.reopen(question) }.controlSize(.small)
        }
        .padding(10)
        .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 9))
    }

    private var answer: String {
        switch model.staged[model.stagedKey(question)] {
        case .option(let i): return question.options.indices.contains(i) ? question.options[i].label : ""
        case .text(let t): return t
        case nil: return ""
        }
    }
}

private struct QuestionCard: View {
    @Environment(AppModel.self) private var model
    var question: Question
    @State private var other = ""
    @State private var typing = false

    var body: some View {
        let key = model.stagedKey(question)
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("\(question.revised == true ? "Updated after discussion" : question.kindLabel) · \(question.id)")
                    .font(.caption.monospaced())
                    .foregroundStyle(Color.amber)
                Spacer()
                Button(discussLabel) { model.toggleDiscussion(question) }
                    .buttonStyle(.link)
                    .font(.caption)
            }
            Text(question.title).font(.headline).fixedSize(horizontal: false, vertical: true)
            if !question.context.isEmpty {
                Text(question.context).font(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            VStack(spacing: 6) {
                ForEach(Array(question.options.enumerated()), id: \.offset) { i, option in
                    OptionButton(option: option, picked: model.staged[key] == .option(i)) {
                        model.stage(question, .option(i))
                    }
                }
                if typing {
                    HStack {
                        TextField(question.options.isEmpty ? "Your answer" : "Another answer", text: $other)
                            .textFieldStyle(.roundedBorder)
                            .onSubmit(submitOther)
                        Button("Use this", action: submitOther).disabled(other.trimmingCharacters(in: .whitespaces).isEmpty)
                    }
                } else {
                    Button(question.options.isEmpty ? "Done / answer…" : "Something else…") { typing = true }
                        .buttonStyle(.link)
                        .font(.caption)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            Text(question.ifNoAnswer)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 2)
            if model.discussing.contains(key) {
                Discussion(thread: model.brief?.thread(for: question.id),
                           placeholder: "Ask about this question…") { text in
                    await model.discuss(question, text)
                }
            }
        }
        .padding(12)
        .background(.background, in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.amber.opacity(0.45)))
    }

    private var discussLabel: String {
        if model.discussing.contains(model.stagedKey(question)) { return "Hide discussion" }
        let n = model.brief?.thread(for: question.id)?.messages.count ?? 0
        return n > 0 ? "Discussion (\(n))" : "Discuss"
    }

    private func submitOther() {
        let text = other.trimmingCharacters(in: .whitespaces)
        guard !text.isEmpty else { return }
        model.stage(question, .text(text))
        typing = false
    }
}

private struct OptionButton: View {
    var option: Question.Option
    var picked: Bool
    var action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    if picked { Image(systemName: "checkmark").font(.caption.weight(.bold)) }
                    Text(option.label).fontWeight(.semibold)
                    if option.recommended == true {
                        Text("RECOMMENDED")
                            .font(.system(size: 9, weight: .semibold, design: .monospaced))
                            .foregroundStyle(Color.amber)
                            .padding(.horizontal, 4)
                            .overlay(RoundedRectangle(cornerRadius: 3).strokeBorder(Color.amber.opacity(0.5)))
                    }
                }
                if !option.effect.isEmpty {
                    Text(option.effect).font(.callout).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 10)
            .padding(.vertical, 8)
            .background(Color.primary.opacity(hovering ? 0.07 : 0.035), in: RoundedRectangle(cornerRadius: 8))
            .overlay(RoundedRectangle(cornerRadius: 8)
                .strokeBorder(hovering || picked ? Color.amber.opacity(0.7) : Color.primary.opacity(0.08)))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
    }
}

/// A side discussion: its messages, Claude's progress, and a box to write in. Nothing here reaches the main chat.
struct Discussion: View {
    var thread: SideThread?
    var placeholder: String
    var send: (String) async -> Void
    @State private var text = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Only here, not in the main chat")
                .font(.caption2.monospaced())
                .foregroundStyle(.tertiary)
            ForEach(Array((thread?.messages ?? []).enumerated()), id: \.offset) { _, m in
                Text(m.text)
                    .font(.callout.weight(m.role == "user" ? .semibold : .regular))
                    .foregroundStyle(m.role == "user" ? Color.primary : Color.secondary)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if thread?.busy == true {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.mini)
                    Text("Claude is answering…").font(.caption).foregroundStyle(.secondary)
                }
            }
            if let error = thread?.error {
                Text(error).font(.caption).foregroundStyle(.red)
            }
            HStack {
                TextField(placeholder, text: $text, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...4)
                    .onSubmit(submit)
                Button("Ask", action: submit)
                    .disabled(thread?.busy == true || text.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding(10)
        .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 8))
    }

    private func submit() {
        let message = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !message.isEmpty, thread?.busy != true else { return }
        text = ""
        Task { await send(message) }
    }
}

/// One step of the turn: a tool call with how it ended, or a note Claude wrote.
private struct ActivityRow: View {
    var event: Activity.Event
    var emphasized: Bool

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: icon)
                .font(.caption)
                .foregroundStyle(.secondary)
                .frame(width: 16)
            VStack(alignment: .leading, spacing: 2) {
                Text(event.text)
                    .font(event.kind == "text" ? .callout : .callout.weight(emphasized ? .semibold : .regular))
                    .foregroundStyle(event.kind == "text" ? Color.secondary : Color.primary)
                    .lineLimit(emphasized ? 6 : 3)
                    .textSelection(.enabled)
                if let command = event.command {
                    Text(command).font(.caption.monospaced()).foregroundStyle(.tertiary)
                        .lineLimit(emphasized ? 4 : 2)
                        .textSelection(.enabled)
                }
                if let note = event.note {
                    Text(note).font(.caption).foregroundStyle(event.status == "blocked" ? .orange : .red)
                        .lineLimit(3)
                        .textSelection(.enabled)
                }
            }
            Spacer(minLength: 4)
            status
            if let at = event.at {
                Text(Date(timeIntervalSince1970: at), format: .dateTime.hour().minute())
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 4)
    }

    @ViewBuilder private var status: some View {
        switch event.status {
        case "running": ProgressView().controlSize(.mini)
        case "failed": Image(systemName: "xmark.circle.fill").foregroundStyle(.red).help("Failed")
        case "blocked": Image(systemName: "hand.raised.fill").foregroundStyle(.orange).help("Blocked")
        default:
            if event.kind == "tool" { Image(systemName: "checkmark").font(.caption).foregroundStyle(.green) }
        }
    }

    private var icon: String {
        switch event.tool {
        case nil: return "text.bubble"
        case "Bash": return "terminal"
        case "Read": return "doc.text"
        case "Edit", "Write", "MultiEdit", "NotebookEdit": return "pencil"
        case "Grep", "Glob", "WebSearch": return "magnifyingglass"
        case "WebFetch": return "globe"
        case "Agent", "Task": return "person.2"
        default: return "wrench.and.screwdriver"
        }
    }
}

// MARK: - Brief

private struct BriefTab: View {
    @Environment(AppModel.self) private var model
    @AppStorage("brief.rules.open") private var rulesOpen = true
    @AppStorage("brief.facts.open") private var factsOpen = true
    @State private var adding = false
    @State private var newRule = ""

    var body: some View {
        if let b = model.brief {
            if let goal = b.goal {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Goal").font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    Text(goal).font(.callout.weight(.medium)).fixedSize(horizontal: false, vertical: true)
                }
            }
            Text("Edit anything that's wrong; Claude gets the change at its next step.")
                .font(.caption).foregroundStyle(.tertiary)
            Section(title: "Rules", count: b.rules.count, recent: b.rules.filter { b.isRecent($0.turn) }.count,
                    open: $rulesOpen) {
                ForEach(b.rules) { r in ItemRow(item: r, kind: "rule", recent: b.isRecent(r.turn)) }
                if adding {
                    HStack {
                        TextField("e.g. Keep the run under 10 minutes", text: $newRule)
                            .textFieldStyle(.roundedBorder)
                            .onSubmit(addRule)
                        Button("Add", action: addRule)
                    }
                } else {
                    Button("+ Add a rule") { adding = true }.buttonStyle(.link).font(.callout)
                }
            }
            Section(title: "Facts", count: b.facts.count, recent: b.facts.filter { b.isRecent($0.turn) }.count,
                    open: $factsOpen) {
                if b.facts.isEmpty { Text("None yet").font(.callout).foregroundStyle(.tertiary) }
                ForEach(b.facts) { f in ItemRow(item: f, kind: "fact", recent: b.isRecent(f.turn)) }
            }
        }
    }

    private func addRule() {
        let text = newRule
        newRule = ""
        adding = false
        Task { await model.editItem("rule", id: nil, text: text) }
    }
}

private struct Section<Content: View>: View {
    var title: String
    var count: Int
    var recent: Int
    @Binding var open: Bool
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                withAnimation(.snappy(duration: 0.2)) { open.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "chevron.right")
                        .font(.caption2.weight(.bold))
                        .rotationEffect(.degrees(open ? 90 : 0))
                        .foregroundStyle(.tertiary)
                    Text(title.uppercased()).font(.caption.monospaced()).foregroundStyle(.secondary)
                    Text("\(count)").font(.caption2.monospacedDigit()).foregroundStyle(.tertiary)
                    if recent > 0 {
                        Text("\(recent) new")
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(Color.accentColor)
                            .padding(.horizontal, 5)
                            .background(Color.accentColor.opacity(0.12), in: Capsule())
                    }
                    Spacer()
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if open {
                VStack(alignment: .leading, spacing: 4) { content }
            }
        }
        .padding(.top, 6)
        .overlay(alignment: .top) { Divider() }
    }
}

/// A rule or fact: its text, where it comes from, examples; Edit corrects it, the menu removes it.
private struct ItemRow: View {
    @Environment(AppModel.self) private var model
    var item: BriefItem
    var kind: String
    var recent: Bool
    @State private var editing = false
    @State private var draft = ""
    @State private var hovering = false

    var body: some View {
        if editing {
            HStack {
                TextField("", text: $draft).textFieldStyle(.roundedBorder).onSubmit(save)
                Button("Save", action: save)
                Button("Cancel") { editing = false }
            }
        } else {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                VStack(alignment: .leading, spacing: 3) {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(item.text).font(.callout).fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled)
                        if recent {
                            Text(item.by == "you" ? "edited" : "new")
                                .font(.caption2.weight(.medium)).foregroundStyle(Color.accentColor)
                        }
                    }
                    if kind == "fact" {
                        HStack(spacing: 6) {
                            SourceTag(source: item.source)
                            if let ex = item.examples, !ex.isEmpty {
                                Menu("Examples (\(ex.count))") {
                                    ForEach(ex, id: \.self) { e in Text(e) }
                                }
                                .menuStyle(.borderlessButton)
                                .font(.caption)
                                .fixedSize()
                            }
                        }
                    }
                }
                Spacer(minLength: 4)
                Button("Edit") {
                    draft = item.text
                    editing = true
                }
                .buttonStyle(.link)
                .font(.caption)
                .opacity(hovering ? 1 : 0)
            }
            .padding(.vertical, 4)
            .padding(.horizontal, 6)
            .background(recent ? Color.accentColor.opacity(0.08) : (hovering ? Color.primary.opacity(0.04) : .clear),
                        in: RoundedRectangle(cornerRadius: 6))
            .onHover { hovering = $0 }
            .contextMenu {
                Button("Edit") { draft = item.text; editing = true }
                Button("Remove", role: .destructive) { Task { await model.removeItem(kind, id: item.id) } }
            }
        }
    }

    private func save() {
        editing = false
        guard draft != item.text else { return }
        Task { await model.editItem(kind, id: item.id, text: draft) }
    }
}

private struct SourceTag: View {
    var source: String?

    var body: some View {
        let (label, color): (String, Color) = switch source {
        case "you": ("you said", .accentColor)
        case "assumed": ("assumed", .amber)
        default: ("found", .teal)
        }
        Text(label)
            .font(.caption2.monospaced())
            .foregroundStyle(color)
            .padding(.horizontal, 5)
            .background(color.opacity(source == "assumed" ? 0 : 0.12), in: RoundedRectangle(cornerRadius: 3))
            .overlay {
                if source == "assumed" {
                    RoundedRectangle(cornerRadius: 3).strokeBorder(color.opacity(0.6), style: StrokeStyle(dash: [2, 2]))
                }
            }
    }
}
