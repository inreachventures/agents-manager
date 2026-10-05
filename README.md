# wm: the task layer for Claude Code

**One ticket. Every repo it touches. One Claude Code conversation, kept on the rails.**

Other agent tools model a *session*: one agent, one repo, one branch. `wm` models a *task*: one ticket, N repos,
one conversation, N PRs and one definition of done.

- 🎙️ **Describe the task by voice or text.** Claude writes the brief and picks the repos it needs.
- 🌳 **An isolated git worktree per repo**, on a branch named after the ticket. Your main checkouts stay clean.
- 🛡️ **Strong guardrails.** No edits in your main checkouts, no branch switching, and **no agent writing over
  another's branch**.
- 🔄 **Live git status integration.** Git, PR and CI state for every repo, plus commits behind `main`, refreshed in
  the background.
- 🚦 **One dashboard for every task.** Who is waiting on you, and **what each repo needs next**, from "needs push"
  to "ready to merge".
- 🧹 **Archive that never loses work.** It refuses while anything is uncommitted or unpushed.
- 💻 **The real `claude` CLI on your subscription.** Local and terminal-native: no API keys, no cloud, no daemon.

![The wm dashboard: ten workstreams with their status, and the git, PR and CI state of every repo](assets/dashboard.png)

## Quick start

Requires macOS, git, tmux, Claude Code, and optionally `gh` for PR and CI status.

```sh
brew install tmux
git clone https://github.com/morenobonaventura/agents-manager.git ~/code/agents-manager && cd ~/code/agents-manager
python3.12 -m venv .venv && .venv/bin/pip install -e .   # or: uv tool install -e .
ln -sf "$PWD/.venv/bin/wm" ~/.local/bin/wm               # Claude sessions call `wm` during intake
wm doctor                                                # checks tools, PATH and Claude folder trust

wm new
```

`wm new` drops you into a Claude session. Describe the task:

> "This is PROJ-313. Add a dark mode toggle: acme-api needs to store the preference and the web UI needs the
> toggle. Done when both are merged."

Claude names the workstream (`PROJ-313 dark mode toggle`), writes `TASK.md`, attaches `acme-api` and `acme-web` as
worktrees on `proj-313-dark-mode-toggle`, summarises the plan and waits for your **"go"**. Then get on with other
work:

```sh
C-b h                  # dashboard: every workstream, its status, git state and PRs
C-b n                  # jump to the next session waiting on you
C-b d                  # detach; sessions keep running
wm                     # reopen the dashboard from any terminal
wm archive PROJ-313    # when the PRs are merged: safety check, stop the session, remove the worktrees
```

## How wm compares

| | **wm** | Claude Code, built in | Agent Deck | Agent of Empires | Conductor | Claude Squad |
|---|:-:|:-:|:-:|:-:|:-:|:-:|
| One ticket across several repos: a worktree per repo, one conversation | ✅ | ◐ ¹ | ✅ | ✅ | ❌ | ❌ |
| The agent attaches repos itself mid-task, the manager creates worktree and branch | ✅ | ◐ ² | ❌ | ❌ | ❌ | ❌ |
| A task brief written at intake that survives compaction and resume | ✅ | ◐ ³ | ❌ | ❌ | ❌ | ❌ |
| Guardrails: no edits in your main checkouts, no branch switches, "do this instead" messages, drift detection | ✅ | ◐ ⁴ | ◐ ⁵ | ◐ ⁵ | ❌ | ❌ |
| What each repo needs next, from git + PR + CI, plus commits behind base | ✅ | ◐ ⁶ | ❌ | ❌ | ✅ ⁷ | ❌ |
| Rebase every repo of the task on request, conflicts handed back to the agent | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Archive that refuses to lose work, unarchive into the same conversation | ✅ | ◐ ⁸ | ◐ | ◐ | ◐ | ❌ |
| Adopt work in progress (branch and uncommitted changes) from a normal checkout | ✅ | ❌ | ◐ | ◐ ⁹ | ◐ ⁹ | ❌ |
| Sessions outlive the terminal, with "needs you" status | ✅ | ✅ | ✅ | ✅ | ◐ | ◐ |
| Real `claude` CLI on your subscription, in your terminal | ✅ | ✅ | ✅ | ✅ | ◐ ¹⁰ | ✅ |

✅ yes · ◐ partly · ❌ not in the project's docs. Checked against each project's public docs on 2 October 2026. These
tools move fast; if a cell is wrong, please open an issue.

1. `--add-dir` shares other repos without isolating them; `--worktree` covers only the repo you launch from;
   multi-repo threads exist in cloud Projects (beta).
2. `/add-dir` gives access to another folder, without a worktree or a branch.
3. Cloud Projects only.
4. Deny rules and hooks to build your own policy; `--worktree` sessions can't write to the main checkout.
5. Container sandboxes.
6. A PR badge for the current branch of one repo.
7. One repo per workspace.
8. One repo at a time: `claude rm` refuses unpushed commits.
9. Starting from an existing branch; uncommitted changes aren't carried over.
10. A macOS app built around Claude Code, not a terminal tool.

**wm doesn't replace Claude Code's agent view.** Agent view is a great way to watch plain sessions. `wm` adds the
layer Claude Code leaves to you: what a session is for, which repos it may touch, and when the task is done.

## How it works

A workstream is one task: a key (`PROJ-313`, or `WS-7` without a ticket), a 3-word name, and a folder holding one
git worktree per repo and exactly one Claude Code conversation at its root:

```text
~/workstreams/PROJ-313/          ← Claude's working directory
    CLAUDE.md                    ← generated by wm: identity, repos, branches, rules
    TASK.md                      ← the brief Claude writes at intake: goal, done-when, decisions
    .claude/settings.json        ← generated: status hooks, guard hooks, deny rules
    acme-api/                    ← git worktree on proj-313-dark-mode-toggle
    acme-web/                    ← git worktree on proj-313-dark-mode-toggle
```

**The agent asks, `wm` builds.** Claude never creates worktrees, switches branches or deletes anything. When it
needs another repo it runs `wm add-repo`, and `wm` creates the worktree and the branch.

- **Voice-first intake.** Claude names the task, writes `TASK.md` and attaches the repos, fuzzy-matching dictated
  names.
- **Survives compaction.** `CLAUDE.md` and `TASK.md` reload after compaction and on resume, with live git and PR
  state.
- **Guardrails that redirect.** Edits outside the workstream, git writes in `~/code/<repo>`, `git switch`,
  `checkout`, `worktree` and `clone` are blocked **with a "do this instead" message**. Drift is caught after every
  command.
- **The next step for each repo.** `✎ uncommitted`, `↑ needs push`, `needs PR`, `✗ CI failing`,
  `● ready to merge`, `✓ merged`, plus "N behind base".
- **Rebase the whole task.** Every branch behind its base, force-pushed with a lease. **Conflicts go back to
  Claude.**
- **Archive that won't lose work.** **Refuses on uncommitted, unpushed or post-merge commits.** `wm unarchive`
  brings it all back.
- **Adopt work in progress.** `--adopt` moves a branch and its uncommitted changes from a normal checkout into the
  workstream, and **rolls back if any step fails**.
- **Outlives your terminal.** Sessions run in `wm`'s own tmux server, with "needs you" status and macOS
  notifications.

## Commands

```sh
wm                 # dashboard (inside the manager's own tmux server)
wm new             # new workstream → you land in Claude; describe the task by voice/text, name the repos
wm new PROJ-313 -n "dark mode toggle" -r web -r api -t "task text"   # skip most of intake
wm ls              # list with git + PR state (--fetch to refresh origin first, --all to include archived)
wm open PROJ-313   # jump into a session (resumes it if stopped)
wm add-repo WS-7 acme-mobile           # attach another repo mid-work (Claude does this itself when asked)
wm add-repo WS-7 acme-mobile --adopt   # bring in work in progress from ~/code/acme-mobile
wm rebase PROJ-313 # rebase branches that are behind their base; force-push (with lease) the ones on origin
wm archive PROJ-313                    # safety report, then stop session + remove worktrees
wm unarchive PROJ-313 --open           # recreate the worktrees on the same branches and resume the conversation
```

Dashboard: `enter` open · `n` new · `a` add repo · `r` resume · `b` rebase · `x` archive · `p` refresh PRs ·
`v` archived view (`u` restore) · `q` exit (sessions keep running).

Optional config in `~/.agents-manager/config.toml`:

```toml
code_roots = ["~/code"]            # where your repos live; their checkouts are protected
workstreams_dir = "~/workstreams"  # where workstream folders are created
```

## Limits

- **macOS and Claude Code only.** Notifications use `osascript`; status and guardrails are built on Claude Code hooks.
- **The shell-command check is best-effort.** A command written to get around it can slip past; drift detection
  catches the result, not the attempt. There's no OS-level sandbox yet.
- **Repo-level Claude settings don't apply.** Each repo's `CLAUDE.md` is loaded, but its `.claude/settings.json` and
  `.mcp.json` are not, because Claude Code only reads those from the working directory.
- **No diff viewer.** Review in your editor or on the PR. PR and CI state needs `gh` and GitHub.

## Disclaimer

**Use at your own risk.** This software is provided as is, without warranty of any kind (see `LICENSE`). `wm` runs
git commands that create and remove worktrees, delete branches, stash changes, rebase and force-push (with lease),
and it starts Claude Code sessions that change your code. The safety checks reduce the risk of losing work, but they
are not a guarantee. The authors are not responsible for any data loss or other damage resulting from its use. Keep
your work committed and pushed, and keep backups.

## Develop

```sh
.venv/bin/pytest -q
.venv/bin/ruff check src tests
```

`DESIGN.md` explains the reasoning behind each design decision. MIT licensed, see `LICENSE`.
