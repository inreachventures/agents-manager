# Task map: a PR Lens view of a workstream's changes

WS-15. A **Map** view in Workstreams.app's Changes panel. It shows what a workstream's change does to the system:
the components it touches, the code around them, and how data moves through them. It is drawn by a background
Claude process that wm starts after each commit, in [PR Lens](https://github.com/coldteadotai/pr-lens)'s format,
and rendered by PR Lens's own renderer, bundled into the app.

## Why

The Changes panel today is a line diff. To understand what an agent did, you read the diff. PR Lens shows the
shape of a change first: lanes of components, coloured by what changed (green added, amber modified, red removed,
plain for the untouched neighbours that give the blast radius), the calls between them, and step-by-step data
flows. The aim is to see the main components and the effect of a change before reading a line of code, inside the
app where the work is being watched, and while it happens rather than once a PR exists.

PR Lens maps one repo per pull request. A workstream is one task across several repos, so the map here is **one per
workstream**, covering every attached repo, with the calls that cross between them. GitHub cannot show that view.

## Goals, non-goals, anti-goals

| | |
|---|---|
| Goals | One map per workstream, across all its repos. The map opens full width in the window's middle, in place of the session. Clicking a card shows its changes in the panel beside it. Architecture and data-flow lenses (plus data-model when the map has one), light and dark following the app. The repo chip narrows the map to one repo. The map says when it is out of date and offers Redraw. It is redrawn after each commit, whichever agent made it, without touching the workstream's session. |
| Non-goals | Attaching the map to PRs (later: a PNG of the whole task map on each repo's PR; GitHub has no image upload API, so this needs a hosting decision). PR Lens's hosted features: review findings, the canvas, payload panels, walkthrough playback. A map in the terminal dashboard (`wm` TUI). One map per repo. |
| Anti-goals | A map that looks current but isn't. Drawing that changes the session's status, sends notifications, raises permission prompts, or adds anything to the session's conversation. Map files committed to a repo. A runtime dependency on Node for the app itself. Unreviewed third-party code running unattended: no npm downloads at runtime. Sending code anywhere new: the map is drawn by Claude on the same subscription that already reads the code, and nothing goes to PR Lens's hosted service. |

## Approaches

**Where the map is rendered**

| | Trade-off |
|---|---|
| **1. In the app (picked)** | PR Lens's renderer is bundled into one JS file, committed, and run in a web view. The app gains no runtime dependency; upgrading PR Lens means re-running the bundle script. |
| 2. In `wm` with Node | `wm` calls PR Lens's CLI to write SVGs and the app displays them. Less bundling, but the app would need Node and an npm download for every render. |
| 3. Natively in SwiftUI | Reimplements a renderer that exists, loses the PR Lens look, weeks of work. |

**Who draws it**

| | Trade-off |
|---|---|
| A background subagent of the session | Rejected after review. Most commits are made by implementer subagents, which would get the nudge and finish before the drawing does. A background subagent also flips a finished session back to working, wakes it when done (a second "your turn" notification per commit) and puts its report in the conversation. |
| **A headless `claude -p` process wm starts (picked)** | Independent of who committed and of the session's state. wm controls its permissions, concurrency and lifetime. It does not share the session's conversation; it reads the code itself, as a subagent would have. |

**What the drawing process may run unattended**

| | Trade-off |
|---|---|
| **Vendored PR Lens, at bundle time (picked)** | The bundle script also builds PR Lens's validator into one JS file and copies its authoring instructions into wm. The drawing process runs only reviewed, committed code. Needs `node` on the machine, no npm. |
| Pre-approved `npx @coldtea/pr-lens-cli@<exact>` | Simpler, but downloads and runs a third-party package with nobody watching. |
| Nothing pre-approved | A permission prompt per drawing; the map would only ever be drawn by hand. |

## What exists today

- `diffs.repo_diff` gives a repo's working tree against `merge-base(base, HEAD)`. `Server.rpc_diff` returns every
  repo's diff plus a digest, and the app polls it while the Changes panel is open.
- `ChangesPanel.swift` is native SwiftUI with no WebKit. The top bar's repo chip sets `model.soloRepo`; the panel
  then shows that repo only.
- `hooks.on_post_tool_use` runs after every Bash call, in the main session and in its subagents, and can emit JSON
  back to Claude (it does for drift). `hooks._transition` moves the session's status on every hook.
- Sessions run with `--allowedTools` from `templates.allowed_tools`; the workstream's `.claude/settings.json`
  carries wm's hooks.
- The app gets its environment from the login shell (`Backend`), so `node` from nvm or Homebrew is on its PATH, and
  `tmux.wm_executable()` gives the absolute path of the `wm` that is running.

## PR Lens facts this design rests on

Checked against `coldteadotai/pr-lens` and npm on 2026-11-17 (schema 0.10.0, renderer 0.6.3, CLI 0.15.2). MIT.

- The graph document (`schemaVersion` 0.3.x): `title`, `summary`, `lenses`, `provenance`, `lanes` (1-16), `nodes`
  (1-256), `edges` (up to 512), `flows` (up to 16), optional `dataModel`, `views`, `walkthrough`, `layout`. Every
  node, edge, flow and step carries a delta: `added`, `modified`, `removed`, `unchanged`. Ids allow `/`.
- `provenance` is required and names **one** repo plus base and head shas (7-40 hex).
- `@coldtea/pr-lens-renderer` depends only on `@coldtea/pr-lens-schema`, which depends only on `zod`. `render(doc,
  { lens, theme, config })` is pure: no network, filesystem or clock. The only Node import (`node:crypto`) is in
  `manifest.ts`, which `render` does not reach.
- Output is a self-contained SVG animated with SMIL, which WebKit plays.
- The schema package exports the parser the CLI's `validate` uses, which reports every problem with its field. The
  authoring instructions are `packages/agent-skill/manual.md` and `references/` (graph document, config, a full
  example).

## Design

### The map's shape across repos

One graph document per workstream. Conventions wm imposes on top of PR Lens's format:

- **Lane ids start with the repo's folder name**: `acme-web/ui`, `acme-api/routes`. A repo may have several lanes;
  each lane belongs to exactly one repo. Lane labels are free text.
- **File paths are workstream-relative**: `acme-api/src/routes/prefs.ts`, so a node's files say which repo they
  are in.
- Edges and flow steps may cross repos (a web client calling an API route). These are the point of a task map.
- `provenance` is filled in by wm, not by Claude: `repo` is `{owner: "wm", name: <workstream key>}` and base/head
  are the first attached repo's merge-base and HEAD. It only exists to satisfy the schema; freshness lives in wm's
  own snapshot (below).

### Vendored PR Lens

`macos/scripts/bundle-prlens.sh`, run by hand when PR Lens is upgraded, with the one pinned PR Lens version:

- Builds `prlens.js` (renderer: `render` and what it reaches) into `macos/Resources/map/`, which `build-app.sh`
  copies into the app.
- Builds `prlens-validate.mjs` (schema parser plus integrity checks, as the CLI's `validate` runs them) into wm's
  package data (`src/agentmgr/data/prlens/`). It takes a file path, prints every problem with its field, and exits
  non-zero on any.
- Copies the authoring instructions (`manual.md`, `references/`) into the same data folder.
- Uses esbuild via `npx`, pinned. Keeps PR Lens's MIT notice at the top of each built file. Smoke-tests both builds
  under Node against PR Lens's own example graph before writing them.

All output is committed. `build-app.sh`, the release bundle and CI stay Node-free. Upgrading PR Lens is: change the
pin, re-run the script, review the diff, commit.

### Drawing: `taskmap.start_drawing` and `wm map save`

A new module (`taskmap.py`) owns the map. wm owns where the file lives and when it was drawn; Claude only writes the
content.

`start_drawing(ws)`:
- If a drawing is in progress, marks it **requested again** and returns.
- If `node` is not on PATH, records that as the last drawing's failure ("the task map needs Node.js") and returns.
- Records a **snapshot**: per attached repo, its HEAD sha, merge-base with its base, and the digest of its diff
  (the per-repo digest `rpc_diff` computes). Stores it as the in-progress drawing with a token and start time.
- Writes the **brief** to `.wm/map/brief.md`: the repos with folders, branches and bases; the vendored instructions'
  path; wm's conventions above; a skeleton document with `provenance` filled; the existing map, if any, to redraw
  from so ids stay stable (PR Lens keeps cards in place when ids persist); the draft path; the validate command
  (`node <absolute path>/prlens-validate.mjs <draft>`); the save command (`<absolute wm> map save <key> --token <t>
  <draft>`).
- Starts `claude -p` detached (its own process group, so the hook returns at once) in the workstream folder, with:
  - the prompt "Draw this workstream's task map: follow `.wm/map/brief.md`";
  - `AGENTMGR_MAP_DRAWING=1` in its environment, so every wm hook it triggers returns immediately: no status change,
    no notification, no guard (permissions below are the guard);
  - `--allowedTools` limited to: reading files anywhere in the workstream folder; read-only git (`git -C <worktree>
    diff --find-renames <fork point>`, and `log|show|status`; `--no-index` is denied); writing the draft file only;
    the validate and save commands exactly as the brief spells them, for the draft only. In `-p` mode anything else
    is denied without a prompt;
  - `GIT_OPTIONAL_LOCKS=0`, so its `git status` never holds `index.lock` while the session commits;
  - output to `.wm/map/last-run.log`.
- Kills the process after 15 minutes, whether or not the app is running: a watchdog started in the same process group
  sends SIGTERM to the group, then SIGKILL 10 seconds later, and notes it in the log. It counts seconds slept, so
  time the Mac spends asleep doesn't count. wm's reaping keeps a wall-clock backstop at an hour, should the watchdog
  itself be gone.

When the process exits, wm (whoever notices first: the server's background tick or the next hook) clears the
in-progress drawing. If it exited without saving, that is the last drawing's failure, with the log's tail; the log
is kept as `failed-run.log`, which "Open log" opens, since the next drawing overwrites `last-run.log`. If it
was **requested again**, and any repo moved since its snapshot (a repo wm can't read doesn't count),
`start_drawing` runs once more. So a burst of commits
costs at most two drawings, and the last one sees the final state.

`wm map save <key> --token <t> <draft.json>` (hidden command, only the drawing process calls it):
- Runs the vendored validator (outside the lock, which a session's commit hook may be waiting on; the token is
  checked again under it before writing), then wm's own checks: every lane id, and every file path on a node or data-model
  table, prefixed by an attached repo's folder. Overwrites
  `provenance`. Rejects with the field at fault, so the process fixes the draft and retries.
- Rejects a token that is not the in-progress drawing's (an old drawing must not overwrite a newer one).
- Writes `.wm/map/map.json` atomically, with the token's snapshot and the save time beside it. `.wm/` is in the
  workstream folder, outside every repo, so nothing is ever committed.

### When it gets drawn

- **After each commit.** `on_post_tool_use`, on a Bash call that ran `git commit` and moved HEAD in one of the
  workstream's worktrees, calls `start_drawing`. "Moved" is against the drawing in progress, else the heads the last
  drawing started from (`.wm/map/attempted.json`, kept even when it failed, so a command that only looks like a commit
  doesn't retry it), else the map. It emits nothing back to Claude. It fires the same way whether the main session or
  one of its subagents committed.
- **Redraw button.** `rpc_redraw_map` calls `start_drawing`. Available whenever the workstream is not archived,
  including while Claude is working and when the session is stopped; disabled while a drawing is in progress.
- The session is never told about the map. Its `CLAUDE.md` does not change.

### Freshness

A map is **current** when, for every attached repo, HEAD and diff digest now equal those in its snapshot. Otherwise
it is **out of date**, and wm says why per repo: "acme-api: 2 new commits", "acme-web: uncommitted changes",
"acme-cli: attached since the map was drawn". A repo removed since is dropped from the comparison.

### Server and app

- `rpc_map(key, since)`: the map document (or none), drawn-at, freshness per repo, drawing in progress since,
  last failure, with the same digest/`unchanged` pattern as `rpc_diff`. Freshness reuses the per-repo diff digests;
  the app polls both on the same tick.
- **Where the map shows.** Not in the changes panel: at its usual width (about 520 points) a real map renders at a
  fifth of its size and can't be read (found on the first real drawing, of this feature's own branch). The map opens
  in the window's middle, in place of the session's terminal, the way canvas tools put the canvas in the main area and
  details in a side pane. A Map toggle in the panel's header and in the toolbar, and View > Show Task Map (⌥⌘M), open
  it; Close or Esc closes it. Open or closed is one app-wide state, not per workstream: switching workstream while it
  is open shows that workstream's map. Opening it moves the changes panel aside (whether it was showing is
  remembered and restored on close, unless the Changes toggle is used while the map is open: that choice stands);
  it closes by itself when no workstream, or an archived one, is selected; the sidebar stays, so sessions' status stays in view. Not a separate window: the
  diff it jumps to lives in the main window, and a second window only covers the first on a laptop screen.
- The map view is a `WKWebView` (`NSViewRepresentable`) loading a bundled HTML page and `prlens.js`. Swift hands it
  the document, lens, theme and repo filter; the page calls `render` and shows the SVG with pan and zoom. Theme
  follows the app's appearance. The page loads nothing from the network.
- Above the map, one line: drawn time; "Up to date" or "Out of date" with the per-repo reasons; "Redrawing…" while
  a drawing is in progress; a Redraw button. A lens switch (Architecture / Data flow, plus Data model when present)
  appears only for lenses the document declares.
- **Repo chip narrows the map.** With a repo picked, the page renders a sub-document holding that repo's lanes and
  nodes, plus any node from another repo that an edge or step connects to it, shown in its own lane as context, so
  a cross-repo call stays visible. Done in the page's JS before `render`.
- **Clicking a card jumps to its diff.** See below.
- Empty and error states: no map yet ("Drawn after the next commit", with Redraw); Node missing; last drawing
  failed (its reason and a way to open the log); a document the renderer rejects (its error, with Redraw);
  archived workstream (as the diff view does today).
- `wm doctor` reports whether `node` is available for the task map.

### From a card to its diff

- **Hit-testing.** The rendered SVG does not carry node ids on its elements, but `render` returns an `atlas` with
  each node's box in viewBox units. The page maps a click into viewBox coordinates (the SVG's screen transform,
  inverted) and finds the node whose box contains it. In the data-flow lens the atlas boxes the participant cards
  heading each column, so those are clickable; flow steps carry no files and are not.
- **What is clickable.** A card is clickable when at least one of its files (workstream-relative, so the first
  segment is the repo folder) is in the current diff. Swift passes the page the set of changed file paths with the
  document; clickable cards get a pointer cursor and a hover outline, the rest stay inert. The untouched neighbours
  that give the blast radius are usually inert, by design.
- **The jump.** The page posts the node's changed files (path plus `startLine` when the map has one) to Swift through
  a `WKScriptMessageHandler`. With one file, Swift jumps straight there; with several, a small menu at the click
  lists them. A jump brings the changes panel back beside the map (the map stays as it is: the page doesn't refit
  when its width changes after a click), narrows the panel to the card's repo (only the panel: the map keeps the
  repo the toolbar chips picked for it, and the panel's own "All repos" and repo headers change only the panel),
  opens the file if it is folded or over the big-file threshold, and scrolls to its row (`<repo>/<path>`). With a
  `startLine`, it scrolls to the hunk whose new-side range covers it, else the nearest hunk below, else the file's header.
- **Scrolling.** `ChangesPanel`'s list gets a `ScrollViewReader`; row ids already address files (`repo/path`) and
  hunks (`repo/path#n`).
- **Stale maps.** If the clicked file has left the diff since the map was drawn (it can, while the map is out of
  date), the card is simply not clickable, because clickability is computed from the current diff.
- **No getting back.** The map never leaves the screen for a jump, so there is no "Back to map" and no pan and zoom to
  restore.
- **Opening zoom.** The page fits the map's width, but never below a readable zoom (0.5): a map too wide for that
  opens from its left edge, to pan or pinch for the rest. Double-click fits again.

## Tests

- Python (`pytest`): `start_drawing` snapshots, writes the brief, starts the process with the env flag and allowed
  tools (process start faked), coalesces a second request, records Node missing; exit without save becomes a
  failure; a requested-again drawing reruns only if repos moved; `map save` runs the validator, rejects bad lane
  prefixes and stale tokens, writes atomically; freshness per repo for new commits, uncommitted work, an added and a
  removed repo; the hook starts a drawing on a commit in a worktree (from the main session and from a subagent's
  payload), ignores other commands and commits elsewhere, and does nothing when `AGENTMGR_MAP_DRAWING` is set;
  `rpc_map`'s digest/unchanged.
- Bundle: the script's smoke tests. CI runs the vendored validator against PR Lens's example graph if `node` is
  present on the runner, so a broken vendored file is caught.
- App: by hand on a real multi-repo workstream: a commit from a subagent produces a map with no status change or
  notification; the repo filter on a cross-repo edge; light and dark; each empty and error state; clicking a card
  with one changed file, with several, with a line range inside a big folded file, an inert unchanged card, in both
  lenses, with the map staying put as the panel opens beside it.

## Left to the builder

- Exact layout under `.wm/map/`, field names in `rpc_map`, and whether drawing state lives in files there or in the
  existing SQLite db.
- The exact `--allowedTools` patterns, within the limits above.
- Whether to pass a model to `claude -p`; default to the CLI's own.
- The HTML page's pan and zoom, and the look of the status line and switches, within the app's existing style.
- The wording of the brief, as long as it carries everything listed above.
