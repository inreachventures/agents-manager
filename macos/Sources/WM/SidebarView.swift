import AppKit
import SwiftUI

/// Workstreams in the TUI's order (needs you, working, your turn, intake, stopped), one row each, with a dot per repo
/// coloured by what it needs next; the selected workstream's repos are in the top bar.
struct SidebarView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        List(selection: $model.selection) {
            if model.showArchived {
                Section("Archived") {
                    ForEach(model.state.archived) { ws in
                        WorkstreamRow(ws: ws)
                            .tag(ws.key)
                            .contextMenu { ArchivedMenu(ws: ws) }
                    }
                }
            } else {
                ForEach(model.state.workstreams) { ws in
                    WorkstreamRow(ws: ws)
                        .tag(ws.key)
                        .contextMenu { WorkstreamMenu(ws: ws) }
                }
            }
        }
        .listStyle(.sidebar)
        .scrollContentBackground(.hidden)
        .background(Color(nsColor: .windowBackgroundColor))
        .id(model.showArchived)  // a fresh list per mode: rows moving between the two lists leave ghosts

        .overlay {
            if model.phase == .ready && model.workstreams.isEmpty {
                Text(model.showArchived ? "Nothing archived" : "No workstreams")
                    .foregroundStyle(.tertiary)
            }
        }
        .safeAreaInset(edge: .top, spacing: 0) {  // the tour's anchor: the list itself doesn't present popovers
            Color.clear.frame(height: 1)
                .tourStop(.sidebar, model: model, anchor: UnitPoint(x: 0.85, y: 1), edge: .trailing)
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
            .tourStop(.archive, model: model, edge: .top)
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
    @Environment(AppModel.self) private var model
    let ws: Workstream

    var body: some View {
        HStack(alignment: .center, spacing: 8) {
            if ws.offersArchive {
                ArchiveButton(label: ws.label) { Task { await model.startArchive(ws.key) } }
            } else {
                StatusIndicator(status: ws.status, unseen: ws.unseen != false)
            }
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
            if ws.status != .archived {
                HStack(spacing: 3) {
                    ForEach(ws.repos) { repo in
                        Circle().fill(Color.tone(repo.tone)).frame(width: 7, height: 7)
                            .help(repo.details)
                    }
                }
                if !ws.badges.isEmpty {
                    HStack(spacing: 4) {
                        ForEach(Array(ws.badges.enumerated()), id: \.offset) { _, badge in BadgeView(badge: badge) }
                    }
                }
            }
            TimelineView(.periodic(from: .now, by: 15)) { context in
                Text(ago(ws.since, now: context.date))
            }
            .font(.caption2.monospacedDigit())
            .foregroundStyle(.tertiary)
        }
        .padding(.vertical, 2)
        .overlay(alignment: .leading) {  // Claude's /color for the session, at the row's edge
            if let color = ws.color {
                Capsule().fill(Color.session(color)).frame(width: 3).padding(.vertical, 1).offset(x: -7)
                    .help("Session colour: \(color) (Claude's /color)")
            }
        }
        .help(([ws.label, ws.statusText] + (ws.drift.map { ["drift: \($0)"] } ?? [])).joined(separator: "\n"))
    }

    @ViewBuilder private var subtitle: some View {
        if let hint = ws.mergeHint, ws.status != .needsYou {
            Text("merged").foregroundStyle(Color.tone("purple"))
                + Text(hint.text.dropFirst("merged".count)).foregroundStyle(Color.tone(hint.tone))
        } else {
            Text(ws.statusText).foregroundStyle(ws.status == .stopped ? Color.secondary : Color.status(ws.status))
        }
    }
}

/// A plugin's mark on a row: its text in a capsule tinted by its tone.
private struct BadgeView: View {
    let badge: WorkstreamBadge
    @Environment(\.backgroundProminence) private var prominence  // .increased: on the selected row's accent color

    var body: some View {
        let tint = Color.tone(badge.tone)
        let selected = prominence == .increased
        Text(badge.text)
            .font(.caption2.weight(.semibold).monospacedDigit())
            .lineLimit(1)
            .padding(.horizontal, 5)
            .padding(.vertical, 1)
            .foregroundStyle(selected ? Color.white : tint)
            .background((selected ? Color.white : tint).opacity(selected ? 0.22 : 0.15), in: Capsule())
            .fixedSize()
            .help(badge.help)
    }
}

/// Takes the status dot's place once a workstream is done: a quiet outline box that fills in under the pointer.
private struct ArchiveButton: View {
    var label: String
    var action: () -> Void
    @Environment(\.backgroundProminence) private var prominence  // .increased: on the selected row's accent color
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            Image(systemName: hovering ? "archivebox.fill" : "archivebox")
                .font(.system(size: 11, weight: hovering ? .semibold : .regular))
                .foregroundStyle(prominence == .increased ? Color.white.opacity(hovering ? 1 : 0.85)
                                 : Color.green.opacity(hovering ? 1 : 0.7))
                .frame(width: 18, height: 18)
                .background(Color.primary.opacity(hovering ? 0.08 : 0), in: RoundedRectangle(cornerRadius: 5))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .animation(.easeOut(duration: 0.12), value: hovering)
        .accessibilityLabel("Archive \(label)")
        .help("Archive")
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
        ColorMenu(ws: ws)
        Divider()
        Button("Reveal in Finder") { model.reveal(ws.folder) }
        Divider()
        Button("Archive…") { Task { await model.startArchive(ws.key) } }
    }
}

/// Claude's /color for the session: wm types `/color <name>` into it when Claude waits at an empty prompt.
private struct ColorMenu: View {
    @Environment(AppModel.self) private var model
    let ws: Workstream

    var body: some View {
        Picker("Colour", selection: Binding(get: { ws.color ?? "default" },
                                           set: { name in Task { await model.setColor(ws.key, name) } })) {
            ForEach(Color.sessionColors, id: \.self) { name in
                Label { Text(name.capitalized) } icon: { Image(nsImage: swatch(name)) }.tag(name)
            }
            Divider()
            Text("Default").tag("default")
        }
        .pickerStyle(.menu)
    }

    /// A coloured dot: menus draw SF Symbols as templates, in the text colour.
    private func swatch(_ name: String) -> NSImage {
        let image = NSImage(size: NSSize(width: 12, height: 12), flipped: false) { rect in
            NSColor(Color.session(name)).setFill()
            NSBezierPath(ovalIn: rect.insetBy(dx: 1, dy: 1)).fill()
            return true
        }
        image.isTemplate = false
        return image
    }
}

struct RepoMenu: View {
    @Environment(AppModel.self) private var model
    let repo: RepoState

    var body: some View {
        ForEach(repo.prLinks) { pr in
            if let url = pr.url { Button("Open PR #\(pr.number)") { model.open(url) } }
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
