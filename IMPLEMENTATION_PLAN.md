# Implementation plan

Implements `DESIGN.md`. Python ≥ 3.11 package `agentmgr`, command `wm`, installed into a venv (`.venv`) and linked to `~/.local/bin/wm`
(or `uv tool install -e .`). Runtime dependencies: `git`, `tmux`, `claude`, optionally `gh`.

## Layout

```
pyproject.toml
src/agentmgr/
    cli.py          Typer app — thin; every command calls the service layer
    config.py       paths + user config (~/.agents-manager/config.toml)
    db.py           SQLite schema, connection (WAL), row dataclasses
    repos.py        repo discovery under code roots + fuzzy matching
    gitops.py       git subprocess wrapper: fetch, default branch, branches, worktrees, status
    naming.py       keys, ticket validation, slugs, branch names
    templates.py    generated CLAUDE.md + .claude/settings.json
    workstream.py   service: new / set / add_repo / remove_repo / resume / cleanup_report / archive
    tmux.py         dedicated tmux server (-L agentmgr): sessions, clients, switch/attach, config
    hooks.py        `wm hook <event>` dispatch: status, SessionStart context, guard, drift check
    guard.py        pure functions: is this tool call allowed? (paths, Bash commands)
    gh.py           PR status via gh
    fetcher.py      background fetch of each repo's base branch (behind-base freshness)
    notify.py       macOS notifications
    tui.py          Textual dashboard
    data/tmux.conf  manager tmux config
tests/              pytest; real git repos in tmp dirs, no tmux/claude needed
```

## State

`~/.agents-manager/` (override with `AGENTMGR_HOME`, used by tests):

- `state.db` — SQLite, WAL mode, `busy_timeout` so hooks and the TUI can write concurrently.
- `config.toml` — `code_roots = ["~/code"]`, `workstreams_dir = "~/workstreams"`, `claude_cmd = "claude"`.
- `hooks.log` — errors from hooks (hooks never fail the Claude session).
- `tmux.conf` — copied from package data on every start (so upgrades apply).

Schema:

```sql
workstreams(key PK, ticket, name, session_id, folder, status, status_reason, status_at,
            created_at, archived_at)
repos(ws_key, repo, repo_path, worktree_path, branch, base, created_branch BOOL, added_at,
      PRIMARY KEY(ws_key, repo))
prs(ws_key, repo, number, state, is_draft, review, checks, url, fetched_at, PRIMARY KEY(ws_key, repo))
counters(name PK, value)       -- WS-<n>
```

Status values: `intake`, `working`, `needs_you`, `your_turn`, `stopped`, `archived`.

## Steps

Each step ends with passing tests (or a manual check where tmux/claude are involved).

**Status (2026-09-23):** steps 1–15 implemented; 39 tests pass. Step 16 was run against
throwaway repos with a real Claude session: intake (name, ticket, TASK.md, add-repo, wait for confirmation),
adding a second repo mid-work (Claude called `wm add-repo` on its own), status hooks, archive, and the dashboard's
archive/resume actions all worked. Not yet exercised for real: `gh` PR status against GitHub repos, macOS
notifications, `C-b n`.

Learned while testing:
- Project-level `permissions.allow` in `.claude/settings.json` makes Claude Code show the folder-trust dialog
  for every new folder, so allow rules go on the command line (`--allowedTools`); deny rules and hooks stay in
  settings.json. A trusted ancestor folder (e.g. `~`) then covers all workstream folders.
- Hook-based denials fire before permission rules, so the guard's explanatory message (e.g. "run
  `wm add-repo …` first") is what Claude sees.

1. **Scaffold** — pyproject (hatchling, deps: typer, textual, rapidfuzz), `wm --help`, pytest + ruff.
2. **config + db** — paths, config loading with defaults, schema creation, connection helper. Tests: fresh
   home creates schema; WS counter increments.
3. **naming** — ticket regex, `WS-<n>` keys, `slugify(name)` (3 words, lowercase, ascii), `branch_name(ws)`
   = `<ticket-or-key lowercase>-<slug>`.
4. **repos** — discover git repos (depth 1) under code roots; fuzzy match (exact → normalized
   (`acme ML pipeline` → `acme-ml-pipeline`) → rapidfuzz with ambiguity threshold). Errors list candidates.
5. **gitops** — `fetch`, `default_branch` (origin/HEAD → main/master fallback), `local/remote branches`,
   `branch_checked_out_at`, `worktree_add(new|existing)`, `worktree_remove`, `status` (dirty, ahead, behind,
   unpushed, has upstream), `is_merged(branch, base)`, `commits_since(base)`.
6. **templates** — render `CLAUDE.md` (identity, intake section while no TASK.md, repo table, rules, done-means,
   `@TASK.md`) and `settings.json` (permissions + hooks calling `wm hook <event>`).
7. **workstream service** — `new` (alloc key, folder, templates, DB row, rollback on failure), `set` (name/ticket,
   refresh templates + symlink), `add_repo` (requires name, fuzzy repo, branch checks, worktree, regenerate),
   `remove_repo` (only if no commits since base and clean). Tests with real temp repos + a bare "origin".
8. **tmux + launch** — tmux server wrapper; `new` starts `claude --session-id <uuid>` (plus optional initial
   prompt); `open` (inside tmux → switch-client, outside → attach in the current terminal); `resume`
   (`claude --resume <id>` when the tmux session is gone); `wm` with no args → attach to `home` (dashboard).
   Manual check.
9. **hooks: status + SessionStart** — `wm hook <event>` reads stdin JSON, maps session_id → workstream, updates
   status; SessionStart prints live repo state as additional context. Tests feed JSON on stdin.
10. **guard + drift** — PreToolUse: file tools must target the workstream folder; Bash: block `git worktree`,
    branch switches, `cd`/`-C` into main checkouts; deny subagent worktree isolation. PostToolUse(Bash): branch /
    worktree drift → feedback to Claude + `⚠ drift`. Unit tests on `guard.py`.
11. **gh** — PR status per repo, cached; `n/a` when gh unavailable.
12. **archive** — cleanup report (dirty / unpushed / merged / PR), refuse unless `--force`, kill session, remove
    worktrees, optional branch delete, keep row as archived.
13. **dashboard (Textual)** — table sorted by status, repo sub-rows, key bindings, periodic refresh (DB 1s, PR 60s),
    new/open/resume/archive actions.
14. **tmux polish** — status-right `wm status-line`, `C-b h` home, `C-b n` next needs-you, mouse + pbcopy.
15. **notifications** — on transition to needs_you / your_turn if no client views the session.
16. **End-to-end manual run** on two real repos.

## Testing approach

- `AGENTMGR_HOME`, `workstreams_dir`, `code_roots` pointed at `tmp_path`.
- Fixture builds "origin" bare repos + clones under a fake code root, so fetch/default-branch/worktrees are real.
- tmux, claude and gh are behind small wrappers, not invoked in unit tests (launch disabled with `--no-launch`).
