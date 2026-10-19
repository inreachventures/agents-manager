import SwiftUI

/// The first-launch tour: four small boxes, each pointing at the part of the window it explains.
enum TourStop: Int, CaseIterable {
    case sidebar, session, changes, archive

    var title: String {
        switch self {
        case .sidebar: return "Workstreams, not repos"
        case .session: return "Talk to Claude"
        case .changes: return "Changes"
        case .archive: return "Archive when done"
        }
    }

    var text: String {
        switch self {
        case .sidebar:
            return "Each workstream is one task with its own Claude session. Its git worktrees are kept apart from "
                + "your checkouts and removed when you archive it."
        case .session:
            return "To add a repo, mention it to Claude (\"we also need acme-api\") or press + next to the repos "
                + "above."
        case .changes:
            return "What each branch changes since it left its base, committed or not. Pick a repo above to see "
                + "only its changes."
        case .archive:
            return "Once a piece of work is done, its dot turns into an archive button. Archived workstreams are "
                + "kept here, conversation included. Try it on EXAMPLE-1."
        }
    }
}

extension View {
    /// Shows the tour's box for `stop` pointing at this view while the tour is there.
    func tourStop(_ stop: TourStop, model: AppModel, anchor: UnitPoint = .center, edge: Edge) -> some View {
        popover(isPresented: Binding(get: { model.tourStop == stop }, set: { shown in
            if !shown && model.tourStop == stop { model.endTour() }  // clicked elsewhere: the tour is over
        }), attachmentAnchor: .point(anchor), arrowEdge: edge) {
            TourCard(stop: stop)
        }
    }
}

private struct TourCard: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openSettings) private var openSettings
    let stop: TourStop

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(stop.title).font(.headline)
            Text(stop.text).fixedSize(horizontal: false, vertical: true)
            if stop == .session, let setup = model.setup {
                HStack(spacing: 6) {
                    let roots = setup.codeRoots.map { NSString(string: $0).abbreviatingWithTildeInPath }
                    Text("Repos come from \(roots.joined(separator: ", ")) · \(setup.found.count) found")
                        .foregroundStyle(.secondary)
                    Button("Change…") { openSettings() }.buttonStyle(.link)
                }
                .font(.callout)
            }
            HStack {
                Text("\(stop.rawValue + 1) of \(TourStop.allCases.count)").font(.caption).foregroundStyle(.secondary)
                Spacer()
                if stop != .archive {
                    Button("Skip") { model.endTour() }.buttonStyle(.link)
                }
                Button(stop == .archive ? "Done" : "Next") { model.nextTourStop() }
                    .keyboardShortcut(.defaultAction)
            }
            .padding(.top, 2)
        }
        .padding(16)
        .frame(width: 300)
    }
}
