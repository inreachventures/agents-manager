# Plugins for wm and Workstreams.app

## Why

The maintainers each build features on top of wm. Two of them went straight into core and are now stuck:

- **Task map** (PR #21, closed unmerged): a PR Lens graph of a
  workstream's changes, redrawn in the background after each commit. About 50k lines, most of it the vendored PR Lens
  validator.
- **Brief panel and chat** (branch `ws-brief-panel`, released as pre-release v0.7.0b1): a ledger of questions,
  facts, rules and steps that Claude keeps with `wm ask/fact/rule/...`, shown in a tabbed side panel, plus a chat view
  of the transcript in the main area and side discussions.

Each is something the other maintainer neither wants to run nor review. The goal is a plugin system so each maintainer
writes extensions in their own repo, installs them on their own machine, and core stays free of feature code.

## Goals

- A plugin is a folder. Dropping it into `~/.agents-manager/plugins/` installs it; nothing else to do.
- A plugin can do everything the two motivating features do today, without core code specific to it (see the
  fit table at the end).
- Plugin UI in the app is a web page shown in a `WKWebView`, with a JS SDK (`window.wm`) to talk back to wm and
  the app.
- With no plugins installed, wm and the app behave exactly as they do on main today.
- The task map moves out to a repo of its own, as the first real plugin.

## Non-goals

- An installer, registry or marketplace. `git clone` into the folder is the installer.
- Sandboxing plugins. A plugin is code you chose to install and runs with wm's permissions.
- Plugin backends in other languages, or as separate processes.
- Native Swift plugin UI.
- Plugin views in the terminal TUI (plugin CLI commands do work from the terminal).
- Porting the brief. The spec shows it fits; porting it is its author's call.

## Anti-goals (failure even if it works)

- Feature-specific code left in core, or core changes needed for the next plugin of the same kind.
- A plugin that raises, fails to import, or targets another API version takes down `wm`, a hook, the app-server or
  the app. It must be skipped, logged, and reported once.
- Plugins making every tool call slower. Claude Code runs `wm hook` on every tool call; a plugin that does not handle
  an event must not even be imported for it.

## Constraints

- `wm` ships inside the app as its own Python environment. Plugins can use the standard library and what wm already
  depends on, nothing else. (The task map's validator needs Node.js, which it already checks for and reports.)
- Hooks are short-lived `wm hook <Event>` processes; the app talks to a long-running `wm app-server` over JSON lines;
  the CLI is Typer. Plugins plug into all three.
- Workstream folders already use `.wm/` for wm-owned files; each plugin gets `.wm/<plugin>/`.
- agents-manager is a public repo and plugin repos need not be. Core's PR carries nothing task-map or PR Lens
  specific (the fixture plugin and docs examples are generic).

## Approaches considered

| Decision | Options | Picked | Why |
|---|---|---|---|
| Plugin UI | Web views with a JS SDK; native Swift compiled into the app behind a flag | **Web views** | Only option that keeps plugin code out of the app's source. PR Lens is already JS. Cost: the brief's ~1,400 lines of SwiftUI (`BriefPanel`, `ChatView`) get rewritten as HTML when it is ported. |
| Plugin backend | Python imported into `wm`; separate executables over JSON | **Python in-process** | Hooks run per tool call; an extra process per plugin per call costs 50-100ms each. Both features are already Python built on wm's modules. Cost: plugins depend on a stable `agentmgr.plugin` API. |
| Where plugins live | Each in its own repo cloned into a folder; one shared plugins repo; a `plugins/` folder inside agents-manager | **Own repo each, in `~/.agents-manager/plugins/`**  | Only this keeps each person entirely out of the other's code and PRs. |

## Design

### 1. Plugin folder and manifest

```
~/.agents-manager/plugins/            (AGENTMGR_PLUGINS_DIR overrides it, for tests)
  taskmap/                            a git clone of the task map's repo
    plugin.toml
    plugin.py                         register(api)
    web/map.html, web/...             pages the app shows
    ...                               anything else the plugin needs
```

`plugin.toml`:

```toml
name = "taskmap"          # must equal the folder name; [a-z][a-z0-9-]*
title = "Task map"
version = "0.1.0"
api = 1                   # the plugin API version it was written for
command = "map"           # optional: `wm map ...` loads this plugin
hooks = ["PostToolUse"]   # the hook events plugin.py handles; for others it is never imported
protect = true            # Claude may not edit .wm/taskmap/ (checked on every tool call, so declared here)

[[views]]
id = "map"
title = "Map"
place = "main"            # "main": an option beside Terminal; "panel": a tab beside Changes
page = "web/map.html"
shortcut = "opt+cmd+m"    # optional: a View menu item with this shortcut
```

The manifest is read without importing `plugin.py`, so `wm hook`, `wm <cmd>` and the app can decide cheaply which
plugins to load.

**On and off.** A plugin in the folder is on. `wm plugins` lists plugins with status (on, off, or skipped and why);
`wm plugins off <name>` / `wm plugins on <name>` record the choice in wm's existing config file. The app's Settings
lists the same with a toggle.

**Picking up changes.** Hook and CLI processes are short-lived, so they always see the current folder. The
app-server is long-running and Python can't unload modules, so with each state push it compares a fingerprint of the
plugins folder (each plugin's `plugin.toml` and `plugin.py` mtimes, plus the on/off choices). When it changes, the
app-server regenerates `CLAUDE.md` and `.claude/settings.json` for active workstreams and sends a `plugins_changed`
event; the app then restarts the app-server (`AppModel.restart()` already exists). A dropped-in, pulled, removed or
toggled plugin is live within a few seconds without restarting the app. Running Claude sessions see the new
`CLAUDE.md` at their next start or compaction; new `allowed_tools` apply from the session's next start.

**API version.** `api` is an integer. Additive changes keep it; breaking ones bump it. Core loads only plugins whose
`api` equals its own and skips the rest with a reason.

**Failure isolation.** Every call into a plugin (import, `register`, each handler) is wrapped. On error the plugin is
marked failed for that process, the traceback goes to wm's hook log (hooks) or stderr (app-server), and the app shows
one notice per plugin per app-server run: "Task map plugin skipped: <one line>". Core's own behaviour continues as if
the plugin were absent.

### 2. Python API: `agentmgr.plugin`

`plugin.py` defines `register(api)`. Plugins import only `agentmgr.plugin`; anything else in `agentmgr` is
internal and may change without notice. The module re-exports what plugins need as a stable facade, which grows
(additively) as the two ports need it.

```python
from agentmgr.plugin import Api, HookContext, HookResult, Badge

def register(api: Api) -> None:
    api.hook("PostToolUse", on_post_tool_use)
    api.cli(typer_app)                       # mounted as `wm <command> ...`
    api.request("map", rpc_map)              # callable from this plugin's web views
    api.request("redraw", rpc_redraw)
    api.claude_md(section)                   # (ws) -> markdown | None, appended to the workstream's CLAUDE.md
    api.replace_claude_md("intake_summary", text_fn)   # replace a named core section (see 2.4)
    api.allowed_tools(tools)                 # (ws) -> ["Bash(wm map save WS-7 *)", ...]
    api.badge(badge)                         # (ws) -> Badge | None, on the sidebar row
    api.watch(paths)                         # (ws) -> [Path]; a change tells this plugin's web views
    api.on_archive(cancel)                   # (ws) before worktrees go
    api.every(30, reap)                      # periodic work in the app-server (the task map's drawing reaper)
    api.doctor(check)                        # () -> [problem str] for `wm doctor`
```

Handlers receive a read-only workstream view (`key, name, ticket, folder, status, repos[] with repo, branch, base,
worktree`) and, through the facade, helpers both ports use today: the plugin's data dir (`api.data_dir(ws)` =
`<folder>/.wm/<plugin>/`), repo heads, the fork-point diff, and `api.aux_env()` (2.2).

#### 2.1 Hooks

`api.hook(event, fn)` for any event core installs (`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
`Notification`, `Stop`, `SessionEnd`). The event must also be listed in the manifest's `hooks`.

`fn(ctx: HookContext) -> HookResult | None`. `ctx` has `ws`, `event`, `payload` (Claude Code's JSON) and `aux`
(2.2). `HookResult` fields, all optional:

| Field | Events | Effect |
|---|---|---|
| `context` | SessionStart, UserPromptSubmit, PostToolUse | text added to Claude's context (`additionalContext`) |
| `deny` | PreToolUse | refuse the tool call with this reason |
| `block` | PostToolUse, Stop | `decision: block` with this reason (Stop: Claude keeps going) |
| `waiting_on` | Stop | the session is waiting on something Claude started (e.g. a background job): status becomes "your turn" with this text, but quietly (no notification, not counted as needing you) |

**Order and merging.** Core's checks run first (guard, drift). If core denies or blocks, plugins are not called for
that event. Then plugins run in name order, and only after them does core apply its status change and notification,
so a plugin's `block` or `waiting_on` can stop a "your turn" alert. `context` strings are joined; the first `deny` or `block` wins and later plugins are still called
(their `context` is kept); the first `waiting_on` wins. Core emits one combined hook output.

**Speed.** `wm hook <Event>` reads manifests and imports only plugins that list the event. Each handler gets a time
budget (default 2s); over budget is logged, not killed. A plugin that needs heavy work does it the way the task map
does: start a background process and return.

#### 2.2 Auxiliary sessions

Both features run extra Claude processes in a workstream folder: the task map's drawing process, the brief's side
discussions (`claude -p --resume --fork-session`). Today each has its own environment marker (`AGENTMGR_MAP_DRAWING`,
`AGENTMGR_SIDE`) that core hooks check by name. That becomes one core mechanism:

- `api.aux_env()` returns env vars marking a process as an auxiliary session of this plugin. The plugin starts the
  process itself with them.
- In an auxiliary session, core hooks still run the guardrails (guard, drift), but never change the workstream's
  status, notify, or show the prompt.
- Only the owning plugin's handlers are called, with `ctx.aux = True`. Other plugins don't see it.
- The owning plugin's `.wm/<plugin>/` is writable by its own auxiliary sessions even when protected.

#### 2.3 CLI

`api.cli(app)` takes a `typer.Typer` mounted as `wm <command> ...`, where `command` comes from the manifest. `wm`
imports a plugin only when its command is invoked (and for `wm --help`, which lists plugin commands in their own
help panel). So the task map's `wm map save` keeps its name; the brief's `wm ask` becomes `wm brief ask` (its
author's call on naming when porting; the CLAUDE.md section tells Claude the commands either way).

#### 2.4 CLAUDE.md and settings

- `api.claude_md(fn)` appends a section after core's Rules. Sections are ordered by plugin name.
- `api.replace_claude_md(name, fn)` replaces one of a small set of **named core sections**, which core's template is
  refactored to expose: `task_format` (the TASK.md template), `intake_summary` (intake step 4) and `progress_rule`
  (the "append to Decisions & progress" rule). These are exactly what the brief changes. If two plugins replace the
  same section, the first by name wins and the other is reported. New names are added to the set when a plugin needs
  one; that is an additive API change.
- `api.allowed_tools(fn)` adds pre-approved tools to the session's `--allowedTools`.
- Core already installs every hook event in `.claude/settings.json` with matcher `*`; no change there.

#### 2.5 App-server

- `plugins` request: each plugin's name, title, version, status, views, and the skip reason if any; and the plugins
  folder (`dir`), for Settings to reveal.
- `plugin_request {plugin, method, params}` calls the handler registered with `api.request`. Errors come back as
  normal request errors (the app shows them in the view, not as a crash).
- `state` gains `badges: [{plugin, text, tone, help}]` per workstream, from `api.badge`. Badge functions run on every
  state push (every 2s while the app is in front), so they must be cheap; one taking over 50ms is logged.
- `api.watch(fn)`: the server checks the returned paths' mtimes with each state push and, when one changes, sends
  `{"event": "plugin_changed", "data": {"plugin", "key"}}`. Hooks run in other processes, so file changes are how a
  hook's work reaches an open view without polling.
- `send {key, text}` moves into core (it is on `ws-brief-panel` already): types text into the workstream's session via
  tmux. Plugin views reach it through the SDK.

### 3. The app

#### 3.1 Where plugin views appear

```
┌ Sidebar ──────┬ toolbar: [Terminal | Chat]       ┬ side panel: [Changes | Map | Brief] ┐
│ WS-51  ② ●    │                                   │                                     │
│ WS-50     ●   │  main area: the terminal, or a    │  Changes is built in; Map, Brief    │
│  ↑ plugin     │  plugin's main view               │  are plugin panel views             │
│    badge      │                                   │                                     │
└───────────────┴───────────────────────────────────┴─────────────────────────────────────┘
```

- **Main views** (`place = "main"`): a segmented picker in the toolbar, Terminal first, shown when at least one main
  view exists, whether or not the session is running (a stopped session's Terminal option shows today's "Session
  stopped" placeholder). While the session runs, the terminal stays attached under the web view (the `ZStack`
  approach from `ws-brief-panel`), so switching back is instant and wm's "is anyone looking at this session" check keeps working. Main
  views are full width; the side panel can sit beside them (the map with a diff open next to it).
- **Shortcuts**: a view with `shortcut` gets a View menu item that selects it.
- **Panel views** (`place = "panel"`): a segmented picker at the top of the side panel, Changes first, shown only
  when at least one panel view exists.
- **Badges**: drawn on sidebar rows from `state.badges`, after the existing status marks.
- The chosen main view and panel tab persist per app (UserDefaults), falling back to Terminal / Changes when the
  plugin is gone.
- No plugins: no pickers, no badges; the window is identical to today's.

#### 3.2 Web view host

One `PluginWebView` (`NSViewRepresentable` over `WKWebView`), generalised from `MapView.swift` on the task-map
branch, which already loads PR Lens this way with a `wm` script message handler.

- Loads `file://<plugin dir>/<page>` with read access to the plugin's folder only.
- Navigation outside that folder is cancelled; `http(s)` links open in the default browser.
- A web content process crash reloads the page once, then shows an error placeholder.
- Views are kept alive per (plugin, view) while their workstream is selected, so switching tabs doesn't reload them.

#### 3.3 The SDK: `window.wm`

Injected as a `WKUserScript` at document start, before the plugin's own scripts.

| Call | What it does |
|---|---|
| `wm.context` | `{plugin, view, key, folder, repos, repo, theme}` for the selected workstream; `repo` is the repo the toolbar's repo chips narrow to, or null for all |
| `wm.request(method, params)` → Promise | `plugin_request` to this plugin's own handlers (a view can't call another plugin's) |
| `wm.on("workstream", fn)` | the selected workstream changed (new `wm.context`) |
| `wm.on("state", fn)` | the selected workstream's row from each state push |
| `wm.on("changed", fn)` | this plugin's watched paths changed for the selected workstream (2.5) |
| `wm.on("theme", fn)` | light/dark changed |
| `wm.on("repo", fn)` | the toolbar's repo selection changed |
| `wm.send(text)` | type into the selected workstream's Claude session (core `send`) |
| `wm.openDiff(repo, path, line?)` | open the side panel on Changes and scroll to that file; from a main view the panel opens beside it (the map's card click) |
| `wm.panel(open)` | show or hide the side panel (the map closes it on a click off the cards, or on Esc) |
| `wm.reveal(path)` | reveal a file in Finder; only paths inside the workstream folder (the map's "Open log") |
| `wm.show(viewId)` | switch to another view of this plugin, or `"terminal"` / `"changes"` |
| `wm.toast(message, level)` | the app's toast |

Theme: the app sets CSS custom properties on `:root` (`--wm-bg`, `--wm-fg`, `--wm-secondary`, `--wm-accent`,
`--wm-border`, `--wm-font`, `--wm-mono`, plus the status tones) and updates them on change, so plugin pages follow
the app's light/dark appearance. Exact names are the builder's call; they are documented in the SDK reference.

### 4. Core changes, in summary

- New: `agentmgr/plugin.py` (public API), `agentmgr/plugins.py` (discovery, manifest parsing, loading, isolation,
  dispatch), `wm plugins` commands, `plugins` / `plugin_request` / `send` requests, `plugin_changed` event, badges in
  state, auxiliary-session handling in `hooks.py`, named sections in `templates.py`, guard protection for plugin dirs.
- App: `PluginWebView`, `wm.js` SDK, view pickers in toolbar and side panel, badges, Settings → Plugins.
- Docs: `docs/plugins.md`, the plugin author's reference (manifest, Python API, SDK, a minimal example).
- A fixture plugin under `tests/fixtures/plugins/` that uses every extension point, used by core's tests.

### 5. The task map as a plugin

A new repo of its own (`wm-taskmap` below), built from the closed branch:

| From the closed branch | Goes to |
|---|---|
| `taskmap.py`, the drawing process | `wm-taskmap/taskmap.py`, using `api.aux_env()` instead of `AGENTMGR_MAP_DRAWING` |
| the app-server's 30s drawing reaper | `api.every(30, ...)` |
| `data/prlens/` (validator, manual, references) | `wm-taskmap/prlens/` |
| `bundle-prlens.sh`, the PR Lens web bundle | `wm-taskmap/scripts/`, output in `wm-taskmap/web/` |
| `MapView.swift` page logic | `wm-taskmap/web/map.html` + JS, a **main** view as on the branch (full width, diff beside it), using `wm.openDiff` for card clicks, `wm.panel(false)` for a click off the cards and Esc, `wm.on("repo")` for the repo chips, `wm.reveal` for Open log |
| toolbar toggle, View > Show Task Map (⌥⌘M) | the main-view picker, `shortcut = "opt+cmd+m"` |
| `_maybe_draw` in hooks | `PostToolUse` handler |
| `map`, `redraw_map` requests | `api.request("map")`, `api.request("redraw")` |
| `wm map save` | `api.cli`, `command = "map"` |
| "Task map" CLAUDE.md section | `api.claude_md` |
| cancel on archive, doctor checks | `api.on_archive`, `api.doctor` |
| tests | `wm-taskmap/tests/`, run against an installed wm |

Nothing task-map-specific lands in agents-manager.

### 6. How the brief fits (for its port)

| Brief touchpoint on `ws-brief-panel` | Plugin API |
|---|---|
| `wm ask/fact/rule/changed/state/step/phase` | `api.cli`, e.g. `wm brief ask` |
| answers handed to Claude at the next tool call / prompt | `PostToolUse` / `UserPromptSubmit` → `HookResult.context` |
| Stop: carry on with new answers; nudge to update status | `Stop` → `HookResult.block` |
| Stop: waiting on a background job, no "your turn" alert | `Stop` → `HookResult.waiting_on` |
| side discussions with their own hook handling | `api.aux_env()` + `ctx.aux` |
| `@.wm/brief` import, intake and TASK.md changes | `api.claude_md`, `api.replace_claude_md("task_format" / "intake_summary" / "progress_rule")` |
| `Bash(wm ask WS-7 *)` etc. pre-approved | `api.allowed_tools` |
| guard blocks edits to `.wm/` | `protect = true` in the manifest |
| `brief`, `brief_answer`, `brief_edit`, `side_*`, `transcript` requests | `api.request` |
| Brief / Needs you / Now tabs | `place = "panel"` views |
| Chat view in the main area | `place = "main"` view, `wm.send` to reply |
| open-question count on sidebar rows | `api.badge` |
| app reloads when the ledger changes | `api.watch` → `wm.on("changed")` |

`rpc_send` and the general "waiting on a job" status move into core as described; everything else in the branch is
plugin code.

## Testing

- Core's existing suite passes with an empty plugins dir.
- The fixture plugin exercises each extension point: hook results merge as specified, CLI mounting, requests,
  CLAUDE.md sections and replacements, badges, watch events, auxiliary sessions, guard protection.
- Isolation: import error, `register` raising, handler raising, wrong `api`, missing manifest fields. Each is
  skipped and reported, and core behaves as with no plugin.
- Laziness: `wm hook PreToolUse` with a plugin that only lists `PostToolUse` does not import it (assert on
  `sys.modules`).
- App: builds in CI; the fixture plugin's views are checked with the existing debug control (load, SDK round trip,
  theme vars, `openDiff`).
- Task map: its own tests in `wm-taskmap`, plus a run against a real workstream where a commit redraws the map and a
  card click opens the diff.

## Sequencing

One plan, core and task map together. The core PR is opened early but stays a draft until the task-map plugin works
end to end against it (a commit redraws the map, a card click opens the diff beside it): the task map is API v1's
first real consumer and should shape it before it ships.

## Left to the builder

- The facade's exact contents beyond what is listed, driven by the two ports.
- The JSON message protocol between `wm.js` and Swift; CSS variable names.
- How `wm plugins off` is stored in the config file.
- Hook handler time budget and badge budget values (2s and 50ms are starting points).

## Release note

v0.7.0b1 shipped from `ws-brief-panel`, not main. The first release from main with plugins does not include the
brief; anyone on 0.7.0b1 loses it until the brief exists as a plugin.
