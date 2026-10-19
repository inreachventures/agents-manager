# Workstreams.app: the native macOS dashboard

The TUI dashboard as a Mac app: workstreams in the sidebar, the selected one's Claude session in the middle, its
repos in the top bar and what their branches change on the right.

- **Welcome guide**: on first launch, three pages: what a workstream is, how it spans repos, and where your repos
  are (the folder that holds them, plus any repo kept elsewhere). Help → Welcome Guide shows it again; Settings →
  Repo Folders… goes straight to the folders.
- **Sidebar**: one row per workstream, in the same order and states as `wm`'s dashboard (needs you, working, your
  turn, intake, stopped). A spinner while Claude works, a pulsing dot when it waits for you (orange with `!` when
  it's blocked on a permission or a question), and a small dot per repo coloured by what it needs next; hover it
  for the details.
- **Top bar**: the selected workstream's repos, each with that dot, its PR number or, from 20 commits, how far
  behind base it is (`acme-web 23↓`). Click one to see only its changes, again to see all; right-click for its PR,
  its worktree and its branch name. ⌘1–⌘9 pick the Nth repo, ⌘0 all of them.
- **Session**: the real `claude` TUI, through a terminal view. Selecting a workstream switches to its session;
  stopped ones offer **Resume**, archived ones **Restore**.
- **Changes** (⌥⌘0): everything each branch changes since it forked from its base: commits, staged, unstaged and
  untracked files.
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

Settings also has "Use Option as Meta key in the terminal": off by default, as in Terminal.app, so Option types the
characters your keyboard layout puts there; on, Option+Enter and Option+letter shortcuts reach Claude instead.

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
  client. wm skips a notification while someone is looking at the session, and the app counts only while it is in
  front. `C-b h` and `C-b n` only say to use the sidebar there; if tmux moves the client anyway, the app moves it
  back. Killing a workstream's session kills its view too.
- **Diffs** (`src/agentmgr/diffs.py`) are read-only: `git diff` against the merge base with the branch's base, plus
  untracked files read from disk, with `--no-optional-locks` so not even the index is refreshed.

## Development

Debug builds (`swift build`, `swift run`) can be driven by a script: with `WM_DEBUG_DIR` set, the app runs the
commands written to `$WM_DEBUG_DIR/cmd` (select a workstream, open a sheet, take a snapshot of its window, …; see
`DebugControl.swift`). Point `AGENTMGR_HOME`, `AGENTMGR_CODE_ROOTS`, `AGENTMGR_WORKSTREAMS_DIR`,
`AGENTMGR_TMUX_SOCKET` and `AGENTMGR_CLAUDE_CMD` at a scratch setup to try it without touching your workstreams.
