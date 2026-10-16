import SwiftUI

private struct SheetFrame<Content: View, Buttons: View>: View {
    var title: String
    @ViewBuilder var content: Content
    @ViewBuilder var buttons: Buttons

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(title).font(.title3.weight(.semibold))
            content
            HStack {
                Spacer()
                buttons
            }
            .padding(.top, 4)
        }
        .padding(20)
        .frame(width: 520)
    }
}

struct NewWorkstreamSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var ticket = ""

    var body: some View {
        SheetFrame(title: "New Workstream") {
            Text("A Claude session opens. Describe the task there, by voice or text: Claude names the workstream, writes TASK.md and attaches the repos, then waits for your go.")
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            TextField("Ticket (optional), e.g. PROJ-313", text: $ticket)
                .textFieldStyle(.roundedBorder)
                .onSubmit(create)
        } buttons: {
            Button("Cancel", role: .cancel) { dismiss() }.keyboardShortcut(.cancelAction)
            Button("Create", action: create).keyboardShortcut(.defaultAction)
        }
    }

    private func create() {
        let ticket = ticket.trimmingCharacters(in: .whitespaces)
        dismiss()
        Task { await model.create(ticket: ticket) }
    }
}

struct AddRepoSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    let key: String
    let repos: [String]
    @State private var query = ""
    @State private var picked: String?

    private var matches: [String] {
        let q = Self.normalize(query)
        guard !q.isEmpty else { return repos }
        return repos.filter { Self.normalize($0).contains(q) || Self.isSubsequence(q, of: Self.normalize($0)) }
            .sorted { Self.normalize($0).hasPrefix(q) && !Self.normalize($1).hasPrefix(q) }
    }

    var body: some View {
        SheetFrame(title: "Attach a repo to \(key)") {
            Text("wm creates the worktree inside the workstream, on the workstream's branch, and adds it to CLAUDE.md and the guardrails.")
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            TextField("Repo name", text: $query)
                .textFieldStyle(.roundedBorder)
                .onSubmit { attach(picked ?? matches.first ?? query) }
            List(matches, id: \.self, selection: $picked) { repo in
                Text(repo).tag(repo)
            }
            .frame(height: 220)
            .contextMenu(forSelectionType: String.self) { _ in } primaryAction: { items in
                if let repo = items.first { attach(repo) }
            }
        } buttons: {
            Button("Cancel", role: .cancel) { dismiss() }.keyboardShortcut(.cancelAction)
            Button("Attach") { attach(picked ?? matches.first ?? query) }
                .keyboardShortcut(.defaultAction)
                .disabled(picked == nil && matches.isEmpty && query.isEmpty)
        }
    }

    private func attach(_ repo: String) {
        guard !repo.isEmpty else { return }
        dismiss()
        Task { await model.addRepo(key, repo: repo) }
    }

    private static func normalize(_ s: String) -> String {
        s.lowercased().filter { $0.isLetter || $0.isNumber }
    }

    private static func isSubsequence(_ needle: String, of hay: String) -> Bool {
        var it = hay.makeIterator()
        return needle.allSatisfy { c in
            while let h = it.next() { if h == c { return true } }
            return false
        }
    }
}

struct RebaseSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    let key: String
    let plan: RebasePlan

    var body: some View {
        SheetFrame(title: "Rebase \(plan.label)?") {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(plan.steps) { step in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Image(systemName: step.skip == nil ? "arrow.triangle.pull" : "minus.circle")
                            .foregroundStyle(step.skip == nil ? Color.accentColor : .secondary)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(step.repo).fontWeight(.medium)
                            if let skip = step.skip {
                                Text("skipped: \(skip)").font(.caption).foregroundStyle(.secondary)
                            } else {
                                Text("\(step.behind) behind \(step.base) → rebase, "
                                     + (step.push ? "then force-push (with lease)" : "not on origin yet, so no push"))
                                    .font(.caption)
                            }
                        }
                    }
                }
            }
            Text("A rebase that conflicts is aborted, leaving the branch as it was, and handed to Claude to resolve.")
                .font(.caption)
                .foregroundStyle(.secondary)
        } buttons: {
            Button("Cancel", role: .cancel) { dismiss() }.keyboardShortcut(.cancelAction)
            Button("Rebase") {
                dismiss()
                Task { await model.rebase(key) }
            }
            .keyboardShortcut(.defaultAction)
        }
    }
}

struct ArchiveSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    let key: String
    let report: ArchiveReport

    var body: some View {
        SheetFrame(title: "Archive \(report.label)?") {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(report.repos) { repo in
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(spacing: 8) {
                            Text(repo.repo).fontWeight(.medium)
                            Text(repo.state).font(.caption).foregroundStyle(Color.tone(repo.tone))
                            Spacer()
                            Text(repo.pr).font(.caption).foregroundStyle(.secondary)
                        }
                        Text(repo.branch).font(.caption.monospaced()).foregroundStyle(.secondary)
                        ForEach(repo.blockers, id: \.self) { blocker in
                            Label(blocker, systemImage: "xmark.circle.fill")
                                .font(.caption)
                                .foregroundStyle(.red)
                        }
                    }
                }
                if report.repos.isEmpty {
                    Text("No repos attached.").foregroundStyle(.secondary)
                }
            }
            Text(report.blocked
                 ? "Archiving now would lose the work above. Commit and push it first, or archive anyway to discard it."
                 : "Stops the session and removes the worktrees; merged branches are deleted. TASK.md and the conversation are kept, so it can be restored.")
                .font(.caption)
                .foregroundStyle(report.blocked ? .red : .secondary)
                .fixedSize(horizontal: false, vertical: true)
        } buttons: {
            Button("Cancel", role: .cancel) { dismiss() }.keyboardShortcut(.cancelAction)
            if report.blocked {
                Button("Archive and Discard Work", role: .destructive) { archive(force: true) }
                    .foregroundStyle(.red)
            } else {
                Button("Archive") { archive(force: false) }.keyboardShortcut(.defaultAction)
            }
        }
    }

    private func archive(force: Bool) {
        dismiss()
        Task { await model.archive(key, force: force) }
    }
}
