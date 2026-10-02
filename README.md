# agents-manager (`wm`)

**Run several Claude Code agents in parallel across all your repos without losing track of them.**
`wm` organises work into *workstreams*. A workstream is one task (usually one ticket, like `PROJ-313`) that can span
several repos, and it has exactly one Claude Code conversation holding all its context. Run `wm new` and describe the
task, by voice or text. Claude names the workstream, writes a brief, and asks `wm` to attach the repos it needs, each
as an isolated git worktree on its own branch. From then on a single terminal dashboard shows every workstream: which
agent is working, which one is blocked on you, and where each PR stands. Everything runs locally in your Claude
subscription, using the real `claude` CLI, with no API keys and no cloud.

### The pain

- **Your work lives in terminal tabs.** Close the wrong tab and the context is gone. With five sessions open, you
  can't tell which is which.
- **Multi-repo tasks get split up.** A change touching `acme-api` and `acme-web` becomes two separate conversations
  that don't know about each other.
- **Worktrees pile up.** Creating them by hand is tedious. Nobody cleans them up, and branches end up named `test2`.
- **You don't know who's waiting on you.** One agent has been sitting on a permission prompt for 20 minutes while
  you watch another one.
- **Agents go off the rails.** They edit your main checkout, switch branches, or start working in a repo that was
  never part of the task.

### How `wm` solves it

- **One task, one conversation, several repos.** Claude runs in `~/workstreams/<KEY>/`, and each repo is a worktree
  subfolder, so all cross-repo context stays in one place.
- **Sessions outlive your terminal.** They run in `wm`'s own tmux server. Close every window and nothing stops. Jump
  into any session from the dashboard with `enter`, or with `wm open PROJ-313`. After a reboot, `r` resumes the same
  conversation.
- **Voice-first intake.** Plain `wm new` asks for nothing up front. Claude picks a 3-word name, records the ticket,
  writes `TASK.md` (goal, done-when, decisions), attaches the repos, reads it all back, and waits for your "go".
- **"Needs you" at a glance.** Claude Code hooks feed live status to the dashboard and the tmux status line
  (`⚠ 2 need you`), and send a macOS notification when a session you aren't looking at gets blocked or finishes.
  `C-b n` jumps to the next one waiting.
- **What each repo needs next.** The Git column combines git and `gh` into one state per repo: `✎ uncommitted`,
  `↑ needs push`, `needs PR`, `✗ CI failing`, `⚠ conflicts`, `awaiting review`, `● ready to merge` (purple),
  `✓ merged` (green), plus problems like a missing worktree or a rebase left half-done. Once a PR is merged, local
  "unpushed"/"behind" counts no longer matter and aren't shown. When everything is merged, the dashboard suggests
  archiving.
- **Knows when `main` moved.** Each repo's base branch is fetched in the background (at most every 5 minutes,
  and on `p`), and again when a session starts. The dashboard shows "N behind base" until the branch is merged,
  highlighted from 20 commits, plus when origin was last checked. Claude is told how far behind it is and when to
  consider a rebase.
- **Rebase on request.** `b` in the dashboard (or `wm rebase PROJ-313`) rebases every branch of the workstream that is
  behind its base, after showing what it will do. Branches already on origin are force-pushed with a lease, so
  commits someone else pushed meanwhile are never overwritten. If a rebase conflicts, `wm` aborts it (nothing
  changes) and asks the workstream's Claude session to resolve it, resuming the session if needed. Nothing is rebased
  without you asking, and not while Claude is mid-task.
- **Cleanup in one step.** `wm archive` stops the session, removes the worktrees and deletes merged branches. The
  brief and the conversation are kept.

### Guarantees against agents going off the rails

- **Structure is created by `wm`, not by Claude.** Worktrees and branches only come from `wm new` / `wm add-repo`,
  so every worktree lives in the workstream folder and every new branch carries the ID (`proj-313-…`). A branch that
  is checked out elsewhere, or doesn't carry the ID, is refused.
- **Your main checkouts are off-limits.** A `PreToolUse` guard hook plus deny rules in the workstream's
  `settings.json` block these:
  - edits outside the workstream folder
  - `cd` or git writes in `~/code/<repo>`
  - `git worktree`, `git switch`, `git checkout <branch>` and `git clone`
  - Claude's own worktree tools
- **Blocks explain what to do instead.** For example: *"acme-mobile is not attached; run `wm add-repo WS-7
  acme-mobile` first"*. Claude corrects itself instead of stalling. In testing it attached a new repo mid-task on
  its own.
- **Drift is detected after every command.** If a worktree changed branch, a stray worktree or clone appeared, or a
  repo went missing, Claude is told to restore it and the dashboard shows `⚠ drift`.
- **No lost work on cleanup.** Archive refuses while any repo has uncommitted changes, unpushed commits, or commits
  made after its PR was merged, unless you pass `--force`, and it only deletes branches that are merged or have no commits of their own.
- **Rules that survive long sessions.** The generated `CLAUDE.md` and your `TASK.md` are reloaded after context
  compaction and on resume. Each session start also injects the live branch, git and PR state of every repo.

*Honest limits:* the shell-command check is best-effort (a command written to get around it can slip past), and
there's no OS-level sandbox yet. Everything else follows your normal Claude Code permission mode.

See `DESIGN.md` for the reasoning and `IMPLEMENTATION_PLAN.md` for the structure.

## Quick start

```sh
# 1. Install (macOS; needs git, Claude Code, and optionally gh for PR status)
brew install tmux
git clone <this repo> ~/code/agents-manager && cd ~/code/agents-manager
python3.12 -m venv .venv && .venv/bin/pip install -e .
ln -sf "$PWD/.venv/bin/wm" ~/.local/bin/wm
wm doctor                      # checks tools, PATH and Claude folder trust

# 2. Start a workstream
wm new
```

`wm new` drops you into a Claude session. Describe the task, by voice or text:

> "This is PROJ-313. Add a dark mode toggle: acme-api needs to store the preference and the web UI needs the
> toggle. Done when both are merged."

Claude names the workstream (`PROJ-313 dark mode toggle`), writes `TASK.md`, attaches `acme-api`
and `acme-web` as worktrees on `proj-313-dark-mode-toggle`, summarises the plan and waits. Say **"go"**.

```sh
# 3. Get on with other work; come back when it needs you
C-b h                          # dashboard: every workstream, its status, git state and PRs
C-b n                          # jump to the next session waiting on you
C-b d                          # detach; sessions keep running
wm                             # reopen the dashboard from any terminal

# 4. When the PRs are merged
wm archive PROJ-313             # safety check, then stop the session and remove the worktrees
```

## Install

Requires macOS, git, tmux (`brew install tmux`), Claude Code, optionally `gh`.

```sh
python3.12 -m venv .venv && .venv/bin/pip install -e .
ln -sf "$PWD/.venv/bin/wm" ~/.local/bin/wm     # Claude sessions call `wm` during intake
wm doctor
```

(`uv tool install -e .` works too once uv is installed.)

## Use

```sh
wm                 # dashboard (inside the manager's own tmux server)
wm new             # new workstream → you land in Claude; describe the task by voice/text, name the repos
wm new PROJ-313 -n "dark mode toggle" -r web -r api -t "task text"   # skip most of intake
wm ls              # list with git + PR state (--fetch to refresh origin first)
wm open PROJ-313    # jump into a session (resumes it if stopped)
wm add-repo WS-7 acme-mobile     # attach another repo mid-work (Claude does this itself when asked)
wm add-repo WS-7 acme-mobile --adopt   # bring in work in progress from ~/code/acme-mobile (see below)
wm rebase PROJ-313  # rebase branches that are behind their base; force-push (with lease) the ones on origin
wm archive PROJ-313 # safety report, then stop session + remove worktrees
wm ls --all        # include archived workstreams
wm unarchive PROJ-313 --open   # recreate the worktrees on the same branches and resume the conversation
```

### Picking up work that isn't in a worktree yet

If you started something in a normal checkout (`~/code/<repo>` on a feature branch, and/or uncommitted changes),
`--adopt` moves it into the workstream. It shows the plan first and asks before doing anything:

1. Stash the uncommitted and untracked changes in that checkout. Ignored files such as `.env` stay where they are.
2. Free the branch there: the main checkout switches back to the default branch; another worktree (`--from <path>`)
   gets a detached HEAD.
3. Create the workstream worktree on that branch, apply the changes, and drop the temporary stash.

If you had uncommitted changes directly on `main`, it creates the workstream branch (`proj-313-…`) from your current
commit and moves the changes onto it. The branch name is kept as is, even without the ticket ID, so upstreams and
open PRs keep working. If any step fails, the checkout is put back exactly as it was. It refuses while a merge or
rebase is in progress, on a detached HEAD, or when the branch already belongs to another workstream.

It works for Claude too: during intake, tell it "continue the work I have in web". It runs
`wm add-repo <key> web --adopt`, shows you the plan, and reruns it with `--yes` once you agree.
Also works at creation: `wm new PROJ-313 -n "dark mode toggle" -r web -r api --adopt`.

Keys inside tmux: `C-b h` dashboard · `C-b n` next workstream that needs you · `C-b d` detach.
Dashboard: `enter` open · `n` new · `a` add repo · `r` resume · `b` rebase · `x` archive · `p` refresh PRs · `v` archived view (`u` restore) · `q` exit (sessions keep running).

Layout: `~/workstreams/<KEY>/` is the Claude working directory, with one git worktree per repo inside it,
a generated `CLAUDE.md` + `.claude/settings.json` (hooks, deny rules) and the `TASK.md` brief Claude writes at intake.

Config (optional) in `~/.agents-manager/config.toml`:

```toml
code_roots = ["~/code"]
workstreams_dir = "~/workstreams"
```

## Develop

```sh
.venv/bin/pytest -q
.venv/bin/ruff check src tests
```

## License

MIT, see `LICENSE`.
