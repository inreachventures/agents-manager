# Writing a wm plugin

A plugin extends `wm` from a folder: Python code that reacts to Claude Code hooks, adds a `wm` command, adds text to each workstream's `CLAUDE.md`, answers requests and shows badges, plus optional web pages that the macOS app shows in the main area or the side panel. Everything a plugin may import lives in one module, `agentmgr.plugin`. Nothing else in `agentmgr` is public, and it can change without notice.

This page is the reference. [The example at the end](#a-complete-example) is a small plugin you can copy.

- [Installing and managing plugins](#installing-and-managing-plugins)
- [The manifest](#the-manifest)
- [The Python API](#the-python-api)
- [Hooks](#hooks)
- [Auxiliary sessions](#auxiliary-sessions)
- [CLAUDE.md](#claudemd)
- [Protecting a plugin's files](#protecting-a-plugins-files)
- [The app-server: requests, badges, watching, jobs](#the-app-server-requests-badges-watching-jobs)
- [Web pages and `window.wm`](#web-pages-and-windowwm)
- [A complete example](#a-complete-example)
- [When something goes wrong](#when-something-goes-wrong)

## Installing and managing plugins

A plugin is a folder in the plugins folder, `~/.agents-manager/plugins/` (`AGENTMGR_PLUGINS_DIR` overrides it). Install one by cloning it there:

```sh
git clone <url> ~/.agents-manager/plugins/<name>
```

Folders whose names start with a dot are ignored. A plugin folder looks like this:

```
~/.agents-manager/plugins/notes/
  plugin.toml        the manifest (required)
  plugin.py          defines register(api) (required)
  util.py            more modules, imported as `from . import util`
  web/
    panel.html       pages named in the manifest's [[views]]
```

Manage plugins from the terminal or the app:

```sh
wm plugins            # every plugin: name, on / off / skipped, version, and the reason when skipped
wm plugins off notes  # turn one off
wm plugins on notes   # and back on
```

In the app, Settings → Plugins lists the same, with a switch per plugin, the reason a plugin was skipped, and a button that reveals the plugins folder in Finder. Turning a plugin off is recorded in `config.toml` as `[plugins] off = ["notes"]`; the rest of that file is left as it was.

How a change is picked up:

| What changed | Effect |
|---|---|
| Any plugin when a hook runs, or a `wm` command | Immediate. Each hook call and each command starts a new process that reads the folder as it is. |
| `plugin.toml` or `plugin.py` of any plugin, a plugin added or removed, a plugin turned on or off | The app-server notices within a couple of seconds, rewrites every workstream's `CLAUDE.md`, and the app restarts the server and reloads every plugin page. Python cannot unload old code, so a restart is the only clean way. |
| `CLAUDE.md` in a running Claude session | Claude Code reads it at session start and after a compaction or `/clear`. A running session sees the new text then; `wm` also regenerates the file on those events. |
| Other Python modules (`util.py`) or web files (`web/*.html`) on their own | Not detected, because only `plugin.toml` and `plugin.py` are watched. Touch `plugin.py`, turn the plugin off and on, or restart the app. |

A plugin must be on to have views. A plugin that is off or skipped shows nothing in the app.

## The manifest

`plugin.toml`, read as TOML without importing any plugin code:

```toml
name = "notes"
title = "Notes"
version = "0.1.0"
api = 1
command = "notes"
hooks = ["PostToolUse", "Stop"]
protect = true

[[views]]
id = "panel"
title = "Notes"
place = "panel"
page = "web/panel.html"

[[views]]
id = "board"
title = "Board"
place = "main"
page = "web/board.html"
shortcut = "opt+cmd+b"
```

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Matches `^[a-z][a-z0-9-]*$` and equals the folder's name. |
| `title` | yes | Display name, non-empty text. |
| `version` | yes | Non-empty text; shown in `wm plugins` and Settings. |
| `api` | yes | The integer `1`, the plugin API version this wm implements. |
| `command` | no | The name of the `wm` subcommand the plugin adds (`wm <command> ...`). Needed for `api.cli`. A name that is already a core command is not mounted, and when two plugins have the same command, only the first by name gets it. The plugin itself stays on; `wm plugins` and `wm doctor` say why its command is missing. |
| `hooks` | no | The Claude Code hook events the plugin handles, from `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Notification`, `Stop`, `SessionEnd`. Default none. |
| `protect` | no | `true` guards `.wm/<name>/` in each workstream (see [Protecting a plugin's files](#protecting-a-plugins-files)). Default `false`. |
| `[[views]]` | no | Web pages, below. |

A view has these keys:

| Key | Required | Meaning |
|---|---|---|
| `id` | yes | Unique within the plugin. |
| `title` | yes | The tab's label. |
| `place` | yes | `"main"` (a tab beside the terminal in the main area) or `"panel"` (a tab in the side panel beside Changes). |
| `page` | yes | The HTML file, relative to the plugin folder. It must stay inside the folder. |
| `shortcut` | no | A View-menu shortcut: modifiers and one key joined by `+`, such as `opt+cmd+d`. Modifiers: `cmd`, `opt`, `ctrl`, `shift`. It must include `cmd` or `ctrl`, so that it cannot take keys typed into the session. Named keys: `return`, `escape`, `space`, `tab`, `delete`, `up`, `down`, `left`, `right`, `home`, `end`, `pageup`, `pagedown`. |

A plugin whose manifest is wrong is skipped, and the reason is shown by `wm plugins`, `wm doctor`, Settings → Plugins and a notice in the app (once per app-server run). The reasons:

| Reason | Cause |
|---|---|
| `cannot read plugin.toml: ...` | The file is missing or unreadable. |
| `plugin.toml is not valid TOML: ...` | A syntax error. |
| `plugin.toml needs a text 'name'` (likewise `title`, `version`) | Missing, not text, or empty. |
| `plugin name '...' must match [a-z][a-z0-9-]*` | A bad name. |
| `plugin name '...' must equal its folder name '...'` | The folder was renamed. |
| `plugin.toml needs an integer 'api'` | `api` is missing or not an integer. |
| `written for plugin API <n>, wm has 1` | A different API version. |
| `command must be text` | |
| `hooks must be a list of event names` | |
| `unknown hook event ...` | An event outside the list above. |
| `views must be [[views]] tables`, `each [[views]] entry must be a table` | |
| `view place must be 'main' or 'panel', not ...` | |
| `view needs a text 'id'` (likewise `title`, `page`) | |
| `view shortcut must be text` | |
| `view page '...' is outside the plugin folder` | A path such as `../x.html`, or a link that leaves the folder. |
| `view id '...' is used twice` | |
| `protect must be true or false` | |

A shortcut that is text but not usable (no `cmd` or `ctrl`, an unknown key, or one the app or another view already uses) does not skip the plugin. The view works and simply has no menu shortcut; the reason is logged by the app.

Plugins that load are also skipped, after the manifest passes, when `plugin.py` fails to import, has no callable `register`, or `register(api)` raises. The reason is then `<ExceptionType>: <message>`. `wm <command>` for such a plugin's command fails with `error: plugin <name> is skipped: <reason>`.

## The Python API

`plugin.py` defines `register(api)`. It runs once per process, in every process that needs the plugin: each hook call for an event it declares, each `wm <command>`, `wm plugins`, `wm doctor`, `wm --help` and the app-server. Keep import time and `register` cheap, and do not do work there beyond registering.

```python
from agentmgr.plugin import Api, Badge, HookContext, HookResult, PluginError, WorkstreamInfo

def register(api: Api) -> None:
    api.hook("PostToolUse", on_tool)
    api.request("list_notes", list_notes)
```

A registration that is wrong (an unknown event, a duplicate, a missing `command`) raises `ValueError` from the `api` method, which skips the plugin with that message.

### How plugins are loaded

The plugin folder is imported as the package `wm_plugin_<name>` (hyphens in the name become underscores), with `plugin.py` as its `plugin` submodule. So:

- Import your own modules with relative imports: `from . import util` or `from .util import helper`.
- The folder's own `__init__.py` is not run, and the package has no `__file__`. Find your folder with `Path(__file__)` inside a module (`plugin.py`, `util.py`), not through the package.
- Two plugins can have modules with the same name without clashing.
- Any exception, including `SystemExit`, while importing or registering skips the plugin; it never ends `wm`.

### Registration methods

Each is called from `register`. "Once" means that a second registration is an error (`api.<name> registered twice`).

| Method | When it runs |
|---|---|
| `api.hook(event, fn)` | `fn(ctx: HookContext) -> HookResult \| None`, on that Claude Code hook event. `event` must be in the manifest's `hooks`. One handler per event. See [Hooks](#hooks). |
| `api.cli(app)` | Mounts a `typer.Typer` as `wm <command> ...`. Needs `command` in the manifest. Once. A plugin's command is loaded only when it is invoked (or for `wm --help`). |
| `api.request(name, fn)` | `fn(**params)` when a web page calls `wm.request(name, params)`. The return value must be JSON-serialisable. A name may be registered once. |
| `api.claude_md(fn)` | `fn(ws: WorkstreamInfo) -> str \| None`, each time a workstream's `CLAUDE.md` is generated. The text is appended. Once. |
| `api.replace_claude_md(section, fn)` | `fn(ws) -> str` replaces a named core section. A section may be replaced once per plugin. |
| `api.allowed_tools(fn)` | `fn(ws) -> list[str]`, when a session starts or resumes (it is passed to Claude Code as `--allowedTools`). The tools are pre-approved for the session, as in `["Bash(wm notes add WS-7 *)"]`. Once. |
| `api.badge(fn)` | `fn(ws) -> Badge \| None` for each active workstream, on every state computation. Once. |
| `api.watch(fn)` | `fn(ws) -> list[Path]`, every state check. Once. |
| `api.on_archive(fn)` | `fn(ws) -> None`, when a workstream is archived, before its worktrees are removed. Once. |
| `api.doctor(fn)` | `fn() -> list[str]`, during `wm doctor`. Return the problems found. Once. |
| `api.every(seconds, fn)` | `fn()` every `seconds` (a positive number) in the app-server. May be registered more than once. |

### Data types

All are plain frozen or simple dataclasses.

- `WorkstreamInfo`: `key`, `name`, `ticket` (either may be `None`), `folder: Path`, `status`, and `repos: tuple[RepoInfo, ...]`.
- `RepoInfo`: `repo`, `branch`, `base`, and `worktree: Path`.
- `HookContext`: `ws: WorkstreamInfo`, `event: str`, `payload: dict` (Claude Code's hook JSON), and `aux: bool` (whether this is the plugin's own [auxiliary session](#auxiliary-sessions)).
- `HookResult`: `context`, `deny`, `block`, `waiting_on`, all `str | None`.
- `Badge(text, tone="", help="")`: `tone` is one of `""`, `"red"`, `"yellow"`, `"blue"`, `"green"`, `"grey"`, `"cyan"`, `"orange"`, `"purple"`, `"amber"` (the constant `TONES`); any other value raises `ValueError`.

### Helpers on `api`

| Helper | Returns |
|---|---|
| `api.name` | The plugin's name. |
| `api.data_dir(ws)` | `<workstream folder>/.wm/<plugin>/` as a `Path`, created if missing. Keep the plugin's per-workstream files here. |
| `api.aux_env()` | `{"AGENTMGR_AUX": "<name>"}`, to add to the environment of an auxiliary session. |
| `api.workstream(key)` | The `WorkstreamInfo` of an active workstream. Raises `PluginError` if there is none or it is archived. |
| `api.workstreams()` | Every active workstream, oldest first. |
| `api.repo_diff(repo)` | What the repo's branch changes since it forked from its base, as the app's Changes view shows it (a dict). |
| `api.git(cwd, *args, check=True)` | stdout of `git` run in `cwd`. Raises `GitError` when it fails and `check` is set. |
| `api.wm_executable()` | The absolute path of the `wm` to call back into. |
| `api.claude_command()` | The command that starts Claude Code. |
| `api.claude_missing()` | `True` when that command is not installed. |
| `api.log(msg)` | Writes `plugin <name>: <msg>` to wm's log (see [When something goes wrong](#when-something-goes-wrong)). |

### Errors

- `PluginError`: raise it for a failure the user should read. A `wm` command prints it as `error: <message>` and exits 1; a web page's `wm.request` rejects with the message alone.
- `GitError` (also `api.GitError`): raised by `api.git`. From a web request it comes back as its message alone, the same as `PluginError`. Any other exception from a request comes back as `<Type>: <message>`.

## Hooks

`wm` registers one command for every Claude Code hook event in each workstream. For an event, it runs in this order:

1. Core's own checks: the guard on `PreToolUse`, drift detection after `Bash` on `PostToolUse`. If core refuses, that is the answer and plugins are not called.
2. The plugins that list the event in `hooks`, in name order. Handlers are isolated: a handler that raises or returns something that is not a `HookResult` of text fields is logged, recorded as its error and left out; one that is slow is logged.
3. Core's status change (working, your turn, ...), which looks at the merged result.
4. One combined output to Claude Code.

A handler has a budget of 2 seconds. One that takes longer is logged, never killed, but every hook call blocks Claude until it returns, so stay well under it.

A hook's standard output is what Claude Code reads, so in a hook process wm sends whatever plugin code prints (while importing, in `register`, in a handler or in a `CLAUDE.md` function) to standard error instead. A process the plugin starts gets standard error in place of standard output too. Use `api.log` for anything you want to read later.

Heavy work belongs in a background process that the handler starts and does not wait for. Start it with its standard streams redirected (`subprocess.DEVNULL` or a log file) and in a session of its own, so that it neither writes into the hook's streams nor keeps them open after the hook returns, which can leave Claude Code waiting on the hook:

```python
import subprocess

def post_tool_use(ctx):
    log = open(api.data_dir(ctx.ws) / "redraw.log", "ab")
    subprocess.Popen(["python3", "-m", "mytool.redraw", str(ctx.ws.folder)], stdin=subprocess.DEVNULL,
                     stdout=log, stderr=log, start_new_session=True)
    return None
```

The fields that each event can carry:

| Event | `context` | `deny` | `block` | `waiting_on` |
|---|---|---|---|---|
| `SessionStart` | added to Claude's context | | | |
| `UserPromptSubmit` | added to Claude's context | | | |
| `PreToolUse` | | refuses the tool call with this reason | | |
| `PostToolUse` | added to Claude's context | | blocks, with this as the reason | |
| `Notification` | | | | |
| `Stop` | | | keeps Claude going, with this as the reason | the session waits on something, see below |
| `SessionEnd` | | | | |

A field that the event cannot carry is dropped. Return `None` (or an empty `HookResult`) to do nothing.

The results of several plugins merge like this: every `context` is joined with a blank line, in plugin name order; for `deny`, `block` and `waiting_on`, the first plugin in name order that sets one wins. Core's checks come before all of it.

**`waiting_on`.** On `Stop`, `waiting_on="text"` tells wm that Claude finished its turn but the real wait is on something Claude started, such as a background job. It is a "your turn" status with that text, and it is quiet: no notification is sent, and the workstream does not count as unseen on the dock badge. Without it, `Stop` is an ordinary "your turn".

**Do not block every `Stop`.** A `Stop` `block` sends Claude back to work, and Claude then stops again, which calls the handler again. A handler that blocks unconditionally loops Claude forever. Claude Code sets `stop_hook_active` in the payload on a `Stop` that follows a block, so check it:

```python
def on_stop(ctx: HookContext) -> HookResult | None:
    if ctx.payload.get("stop_hook_active"):
        return None
    if unfinished(ctx.ws):
        return HookResult(block="Finish the checklist in .wm/notes/todo.md first.")
    return None
```

## Auxiliary sessions

A plugin can start its own Claude Code session in a workstream's folder, for example to run a review in the background. Start it with the environment from `api.aux_env()` (`AGENTMGR_AUX=<plugin name>`), and, like any background process (see [Hooks](#hooks)), with its standard streams redirected and `start_new_session=True`. `wm` treats it as an auxiliary session of that plugin:

- Core still runs the guard (so the auxiliary session cannot edit your main checkouts or switch branches) and drift detection.
- Core skips its status changes and notifications, so the session does not make the workstream look busy or waiting.
- Only the owning plugin's hook handlers are called, with `ctx.aux` set to `True`. Other plugins hear nothing from it.
- The owning plugin's protected `.wm/<plugin>/` folder is writable from its own auxiliary session (see below).

## CLAUDE.md

`CLAUDE.md` in each workstream is generated by `wm` and rewritten when something relevant changes. Plugins affect it in two ways.

**Appending.** `api.claude_md(fn)` returns markdown that is appended after the "Done means" section. Return `None` for nothing. Sections from several plugins are added in plugin name order.

**Replacing.** `api.replace_claude_md(section, fn)` swaps a named section of core's text for `fn(ws)`. The names and what each one replaces:

| Name | Replaces |
|---|---|
| `task_format` | The end of the `TASK.md` template shown during intake: the `## Open questions` and `## Decisions & progress` headings and the line under them. The template's start (key, ticket, name, Goal, Context, Done when, Out of scope) stays. |
| `intake_summary` | Intake step 4, the line that says what to summarise back to the user. |
| `progress_rule` | Two places: the Rules bullet that says to append to "Decisions & progress" at milestones, and, in the Memory section, the bullet that says task facts go under "Decisions & progress". Your text takes the place of the first and the second is dropped. |

If two plugins replace the same section, the first by name wins. The other's replacement is not called, and it is recorded as that plugin's error (`replace progress_rule: already replaced by <plugin>`), which `wm doctor` and the app show until the next time `CLAUDE.md` is written without the clash. A replacement that returns an empty string is ignored.

`api.allowed_tools(fn)` is related: the tools it returns are passed to Claude Code when a session starts, so they run without a prompt. Keep them as narrow as the one command you mean, such as `Bash(wm notes add WS-7 *)`. They are passed joined by commas, so an entry that contains a comma is left out and recorded as the plugin's error.

## Protecting a plugin's files

A plugin that keeps state in `.wm/<plugin>/` may not want Claude editing it by hand. Set `protect = true` in the manifest. Then, in the workstream's main session, the guard refuses `Edit`, `Write`, `MultiEdit` and `NotebookEdit` on anything under `.wm/<plugin>/`, with the message that the file belongs to the plugin and should be changed with its `wm` commands. Give Claude a command (`api.cli`) and pre-approve it with `api.allowed_tools`, so that the plugin's data changes go through code that validates them.

The limits:

- It covers file tools only. A `Bash` command that writes the files is not blocked, and reads are never blocked.
- It is a convenience for steering Claude, not a security boundary.
- The plugin's own auxiliary session may write its folder.
- The guard reads manifests rather than importing plugins. A plugin that is turned off, or whose `plugin.toml` does not parse, loses its protection.

## The app-server: requests, badges, watching, jobs

The macOS app runs `wm app-server`, one long-lived Python process. A plugin's registrations that apply to it:

**Requests.** `api.request("name", fn)` makes `fn(**params)` callable from the plugin's pages. The params are a JSON object, passed as keyword arguments, and the return value must be JSON-serialisable (not `NaN` or infinity either: those fail the request with a `ValueError`). Requests run on a pool of threads, so two can run at once; take care with shared state. Raise `PluginError` (or let a `GitError` through) for an error the page should show. Calling a name the plugin did not register fails with `plugin <name> has no request '<method>'`. Requests do not trigger a state push, so a page that polls does not keep the dashboard refreshing.

**Badges.** `api.badge(fn)` returns a `Badge` for the workstream's sidebar row, or `None`. It runs for every active workstream each time the state is computed, which is every 2 seconds while the app is in front (every 10 in the background), so it must be cheap: read a small file, not the network. A call that takes over 50 ms is logged. A return that is not a `Badge` is recorded as an error and the badge is left out. `text` is short (a count, a word), `tone` colours it, and `help` is its tooltip.

**Watching.** `api.watch(fn)` returns the paths the plugin's pages depend on for a workstream, usually files in `api.data_dir(ws)`. After each state check, wm compares the modification time and size of those paths with the last check. When one changed (written, created or removed), that plugin's pages for that workstream receive the `changed` event. The first look only takes note. Return a list of paths; returning a single string is an error.

**Jobs.** `api.every(seconds, fn)` runs `fn()` every `seconds` on a thread of its own, so a slow or failing job holds up nothing else. An exception is logged and the job runs again at the next tick. Jobs run only while the app (and so the app-server) runs.

**Notices.** When a plugin is skipped, the app shows a warning notice once per app-server run, in the form `<title> plugin skipped: <reason>`. Each handler error (see [When something goes wrong](#when-something-goes-wrong)) gets one notice per app-server run too, in the form `<title> plugin: <handler>: <reason>`, whichever process ran into it: a hook, a `wm` command or the app-server itself.

**Archive and doctor.** `api.on_archive` runs when a workstream is archived (from the app or `wm archive`), before the worktrees are removed. `api.doctor` is described under [When something goes wrong](#when-something-goes-wrong).

## Web pages and `window.wm`

A view's page is an HTML file loaded from the plugin folder. It can read files in that folder and navigate within it. Links to `http` and `https` open in the default browser instead of in the page, `window.open` does the same, and everything else (another folder, other schemes) is refused. Once a page has been shown it stays loaded (hidden) while its workstream is selected, so switching tabs does not reload it, and it is told about another workstream through the `workstream` event. Safari's Web Inspector can attach to it.

Before the page's own scripts run, the app injects the SDK, `window.wm`, the first `wm.context`, and the theme variables.

### Calls

Every call returns a promise.

| Call | Does |
|---|---|
| `wm.request(method, params)` | Calls the plugin's `api.request(method, fn)` handler with `params` (an object, default `{}`), for the plugin that owns the page. Resolves to the handler's return value, rejects with its error message. |
| `wm.send(text)` | Types `text` into the selected workstream's Claude session and presses Enter. Rejects if the session is stopped. |
| `wm.openDiff(repo, path, line)` | Opens the side panel on Changes, narrowed to `repo` and scrolled to `path` (relative to the repo). `line` is optional. Rejects when the workstream is no longer selected or has no such repo. |
| `wm.panel(open)` | Shows (`true`) or hides (`false`) the side panel. |
| `wm.reveal(path)` | Reveals a file in Finder. A relative path is taken from the workstream's folder; the file must exist and lie inside the folder, symlinks resolved, or the call rejects with `outside the workstream` (or `no such file: <path>`). |
| `wm.show(view)` | Switches to another view of this plugin by its manifest `id`, or to `"terminal"` or `"changes"`. Rejects with `plugin <name> has no view '<id>'`. |
| `wm.toast(message, level)` | The app's toast. `level` is `"info"` (default), `"warning"` or `"error"`. |
| `wm.on(event, fn)` | Listens for an event. Returns a function that stops listening. An unknown event throws. |
| `wm.off(event, fn)` | Stops listening. |

Calls that act on a workstream (`send`, `openDiff`, `reveal`) use the one that was selected when the page made the call; with none, they reject with `no workstream is selected`. Arguments must be JSON.

A call rejects with an `Error` whose message is one of:

- the handler's own message, for a `PluginError` or `GitError` in a request, or `<Type>: <message>` for any other exception;
- `wm restarted; try again`, when the app-server was restarting (a plugin changed) while the call was in flight. The page also reloads once the new server is up;
- `wm.request: params must be an object`, `wm.<call>: no <argument>`, and `wm has no call '<name>'`, for a malformed call;
- `wm isn't available on this page`, when the page runs outside the app.

### `wm.context`

A frozen object, replaced (not mutated) when it changes:

```
{
  plugin: "notes",        // this plugin's name
  view: "panel",          // this view's id
  key: "WS-7",            // the selected workstream, or null
  folder: "/Users/.../WS-7",
  repos: [{repo, branch, base, worktree}],
  repo: "acme-api",       // the repo picked in the top bar, or null for all
  theme: "dark"           // "light" or "dark"
}
```

### Events

| Event | Fires when | Data |
|---|---|---|
| `workstream` | another workstream is selected | the new `wm.context` |
| `repo` | another repo is picked in the top bar | `{repo}` |
| `theme` | the app switches between light and dark | `{theme}` |
| `changed` | a path the plugin's `api.watch` returned changed | `{plugin, key}` |
| `state` | the app receives a state push, and once when the page finishes loading | the selected workstream's row |

`state` is the same row the sidebar uses, for example `key`, `status`, `status_text`, `repos` (with each repo's `state`, `pr` and `pr_url`) and `badges`. The app-server pushes the state when anything in it changed, and checks every 2 seconds, so the event can arrive that often. Compare the data with what you rendered and re-render only when it differs, or the page flickers and loses scroll position and focus.

### Theme

The theme is a set of CSS variables on `:root`, resolved for light or dark, and kept up to date when the appearance changes. `color-scheme` is set to match.

| Variable | Is |
|---|---|
| `--wm-font`, `--wm-mono` | The system sans and monospace font stacks. |
| `--wm-bg`, `--wm-fg` | The text background and the primary text colour. |
| `--wm-secondary` | Secondary text. |
| `--wm-accent` | The user's accent colour. |
| `--wm-border` | Separators. |
| `--wm-red`, `--wm-yellow`, `--wm-green`, `--wm-blue`, `--wm-cyan`, `--wm-orange`, `--wm-purple`, `--wm-amber` | The status colours, the same as badge tones. |
| `--wm-grey` | The same colour as `--wm-secondary`. |

The colours come out as `rgba(...)` values. Use the variables rather than fixed colours, and the page follows the app into dark mode.

## A complete example

A scratch-notes panel: a command to add notes, a page that lists them and adds more, a count badge, and a change event when the file is edited.

`~/.agents-manager/plugins/scratch/plugin.toml`:

```toml
name = "scratch"
title = "Scratch notes"
version = "0.1.0"
api = 1
command = "scratch"
protect = true

[[views]]
id = "notes"
title = "Notes"
place = "panel"
page = "web/notes.html"
```

`~/.agents-manager/plugins/scratch/plugin.py`:

```python
import json
from pathlib import Path

import typer
from agentmgr.plugin import Api, Badge, PluginError, WorkstreamInfo

api: Api
cli = typer.Typer(no_args_is_help=True)


@cli.callback()
def main() -> None:
    """Scratch notes kept with a workstream."""


def _file(ws: WorkstreamInfo) -> Path:
    return api.data_dir(ws) / "notes.json"


def _notes(ws: WorkstreamInfo) -> list[str]:
    f = _file(ws)
    return json.loads(f.read_text()) if f.exists() else []


def _add(key: str, text: str) -> None:
    if not text.strip():
        raise PluginError("a note can't be empty")
    ws = api.workstream(key)  # PluginError for an unknown or archived workstream
    _file(ws).write_text(json.dumps([*_notes(ws), text]))


@cli.command()
def add(key: str, text: str) -> None:
    """Add a note to workstream KEY."""
    _add(key, text)
    typer.echo("noted")


def list_notes(key: str) -> list[str]:
    return _notes(api.workstream(key))


def add_note(key: str, text: str) -> dict:
    _add(key, text)
    return {}


def section(ws: WorkstreamInfo) -> str:
    return f"## Scratch notes\n\nKeep notes with `wm scratch add {ws.key} <text>`, not by editing `.wm/scratch/`."


def badge(ws: WorkstreamInfo) -> Badge | None:
    n = len(_notes(ws))
    return Badge(text=str(n), tone="blue", help=f"{n} scratch note(s)") if n else None


def register(a: Api) -> None:
    global api
    api = a
    api.cli(cli)
    api.request("list_notes", list_notes)
    api.request("add_note", add_note)
    api.claude_md(section)
    api.allowed_tools(lambda ws: [f"Bash(wm scratch add {ws.key} *)"])
    api.badge(badge)
    api.watch(lambda ws: [_file(ws)])
```

`~/.agents-manager/plugins/scratch/web/notes.html`:

```html
<!doctype html>
<meta charset="utf-8">
<style>
  body { margin: 0; padding: 12px 16px; background: var(--wm-bg); color: var(--wm-fg); font: 13px var(--wm-font); }
  #error { color: var(--wm-red); }
</style>
<ul id="notes"></ul>
<form id="add"><input id="text" placeholder="Add a note"> <button>Add</button></form>
<p id="error"></p>
<script>
  let shown = null;
  async function show() {
    if (!wm.context.key) return;
    const notes = await wm.request("list_notes", {key: wm.context.key});
    if (JSON.stringify(notes) === shown) return;  // nothing changed: leave the page alone
    shown = JSON.stringify(notes);
    document.getElementById("notes").replaceChildren(...notes.map(n => {
      const li = document.createElement("li");
      li.textContent = n;
      return li;
    }));
  }
  document.getElementById("add").addEventListener("submit", async e => {
    e.preventDefault();
    const input = document.getElementById("text");
    try {
      await wm.request("add_note", {key: wm.context.key, text: input.value});
      input.value = "";
      document.getElementById("error").textContent = "";
    } catch (err) {
      document.getElementById("error").textContent = err.message;
    }
  });
  wm.on("workstream", () => { shown = null; show(); });
  wm.on("changed", show);
  show();
</script>
```

The page asks for the notes on load, when another workstream is selected, and when `notes.json` changes, which `api.watch` reports whether the change came from the page, from `wm scratch add` or from anywhere else. It does not use `wm.on("state")`, so it does no polling. A page that wants status information would use that event, with the comparison shown above.

The `tests/fixtures/plugins/demo` folder in wm's own repository is a larger working example that uses every registration method and every `wm` call.

## When something goes wrong

A plugin's failure never reaches Claude or the dashboard: it is contained to that plugin and its handler.

- **Log.** Hooks log to `hooks.log` in the wm home (`~/.agents-manager/hooks.log`, or under `AGENTMGR_HOME`). It records handler exceptions (`plugin <name>: hook PostToolUse: RuntimeError: boom`), handlers that ran over budget (`plugin <name>: hook Stop took 2.31s`), invalid return values, and skipped plugins. `api.log` writes there too. The app-server logs the same lines to its own standard error instead.
- **Handler errors.** Hooks, `wm` commands and the app-server each run in their own process, so wm keeps each handler's latest error in `plugin-errors.json` in the wm home, one entry per plugin and handler (`hook Stop`, `badge`, `watch`, `claude_md`, `replace progress_rule`, `every 60s`, ...) with the reason and when it happened. An error is an exception, a wrong return value (`returned int, not text`, `returned str, not a list of text`, `returned 5, not a HookResult of text fields`) or a `CLAUDE.md` replacement that clashes with another plugin's. The entry is removed the next time that handler succeeds, in any process. A handler that keeps failing the same way updates the entry's time at most once a minute. Entries of plugins you have removed are dropped; those of a plugin turned off are kept but not shown. The file is wm's, not yours to edit, though deleting it is harmless.
- **`wm doctor`.** Has a Plugins section that lists, per plugin: why it was skipped; each handler error from any process, with how long ago it last happened (`demo: hook Stop: RuntimeError: boom (5m ago)`); a command that clashes with a core command or with another plugin's, or a CLI that cannot be built; and whatever the plugin's own `api.doctor` function returns. A plugin that is loaded but has a broken handler stays loaded: only that handler is affected.
- **The app.** Settings → Plugins shows each plugin's state and the reason it was skipped. A notice appears once per app-server run for each skipped plugin and for each handler error. A page that cannot load shows a placeholder with a Reload button; a page whose web process dies is reloaded once, and a second death within a minute shows the placeholder.
- **A page misbehaves.** Attach Safari's Web Inspector (Develop menu, then the app's name) to read its console.
