# Workstreams.app: the native macOS dashboard

The TUI dashboard as a Mac app: workstreams in the sidebar, the selected one's Claude session in the middle, and
what its branches change on the right.

- **Sidebar**: the same order and states as `wm`'s dashboard (needs you, working, your turn, intake, stopped). A
  spinner while Claude works, a pulsing dot when it waits for you (orange with `!` when it's blocked on a permission
  or a question). Each workstream opens onto its repos, with what each one needs next (`↑ needs push`,
  `● ready to merge`, …) and how far behind base it is once that matters (yellow from 20).
- **Session**: the real `claude` TUI, through a terminal view. Selecting a workstream switches to its session;
  stopped ones offer **Resume**, archived ones **Restore**.
- **Changes** (⌥⌘0): per repo, everything the branch changes since it forked from its base: commits, staged,
  unstaged and untracked files. Each repo has a chip to show or hide its changes; clicking a repo in the sidebar
  scrolls to it.
- **Actions**: the `…` toolbar menu or the context menu, ⌘N new, ⇧⌘A add repo, ⇧⌘B rebase, ⌘⌫ archive, ⇧⌘↩
  resume, ⌘R refresh git/PR/CI, ⌘J next workstream waiting on you, ⌥⌘↑/↓ previous/next, ⌥⌘A archived ones. The
  Dock icon shows how many workstreams wait on you.

## Build and run

Needs Xcode (Swift 5.10+) and macOS 14+.

```sh
macos/scripts/build-app.sh --open     # builds macos/build/Workstreams.app and opens it
```

Copy `Workstreams.app` to `/Applications` if you like. During development, `cd macos && swift run` works too.

The app runs `wm app-server`, so it needs a `wm` that has it. It uses, in order: `$WM_BIN`, the path set in
Settings (⌘,), the `wm` of the checkout it was built from (`.venv/bin/wm`), then the first `wm` on your login
shell's PATH. GUI apps don't get your shell's PATH, so the app reads it from a login shell once at start.

## How it works

The app is only a view. It never runs git, creates a worktree or starts a session itself:

```text
Workstreams.app ──JSON lines──▶ wm app-server ──▶ workstream service (same code as the CLI and the TUI)
      │                                                │
      └── terminal view: tmux client ──▶ wm's tmux server (-L agentmgr) ◀── claude sessions, hooks, guardrails
```

- **`wm app-server`** (`src/agentmgr/server.py`) answers requests (`new`, `add_repo`, `resume`, `rebase_plan`,
  `rebase`, `archive_report`, `archive`, `unarchive`, `refresh`, `diff`, `attach`, `switch`) by calling the
  workstream service, so every check of the CLI applies: archive refuses to lose work, rebase is refused while
  Claude works, repos are fuzzy-matched, worktrees and branches are created and removed only by `wm`. It pushes the
  dashboard state whenever it changes (checked every 2s), refreshes PRs every 60s and fetches bases like the TUI.
- **Sessions stay in wm's tmux server**, so they outlive the app exactly as they outlive a terminal, and the TUI and
  the app can be used side by side. The app shows a workstream through a session grouped with it, `<key>_app`: same
  window (same Claude), but no tmux status line, and its client detaches when Claude exits instead of jumping to
  another session. One terminal view serves the whole window: selecting another workstream switches that tmux
  client, so wm's "skip the notification if someone is looking" check sees exactly the session on screen.
  `C-b h` and `C-b n` only say to use the sidebar there. Killing a workstream's session kills its view too.
- **Diffs** (`src/agentmgr/diffs.py`) are read-only: `git diff` against the merge base with the branch's base, plus
  untracked files read from disk, with `--no-optional-locks` so not even the index is refreshed.

## Development

Debug builds (`swift build`, `swift run`) can be driven by a script: with `WM_DEBUG_DIR` set, the app runs the
commands written to `$WM_DEBUG_DIR/cmd` (select a workstream, open a sheet, take a snapshot of its window, …; see
`DebugControl.swift`). Point `AGENTMGR_HOME`, `AGENTMGR_CODE_ROOTS`, `AGENTMGR_WORKSTREAMS_DIR`,
`AGENTMGR_TMUX_SOCKET` and `AGENTMGR_CLAUDE_CMD` at a scratch setup to try it without touching your workstreams.
