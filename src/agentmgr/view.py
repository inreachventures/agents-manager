"""Read model shared by `wm ls` and the dashboard."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import db, gh, gitops, templates, tmux

SORT = {db.NEEDS_YOU: 0, db.WORKING: 1, db.YOUR_TURN: 2, db.INTAKE: 3, db.STOPPED: 4, db.ARCHIVED: 5}


@dataclass
class RepoRow:
    link: db.RepoLink
    state: str  # what this repo needs next (see git_state); empty when git status wasn't read
    pr: str
    pr_url: str | None  # the PR to open: the open one, else the last merged (gh.RepoPRs.lead)
    merged: bool  # the work reached base (gh.RepoPRs.landed)
    base_ci: str | None = None  # last merged PR: CI of the merge commit on the base branch (pass/fail/pending/None)
    behind: int = 0  # commits on base the branch doesn't have; 0 once merged, where it no longer matters
    style: str = ""  # rich style for `state`
    untouched: bool = False  # no commits, changes or PR: doesn't hold up archiving the workstream
    fetched_ok_at: float | None = None
    fetch_error: str | None = None
    prs: list[db.PR] = field(default_factory=list)  # the PRs the PR column lists (gh.RepoPRs.shown)

    @property
    def git(self) -> str:
        return self.state + (f" · {self.behind} behind base" if self.behind else "")


@dataclass
class Row:
    ws: db.Workstream
    status: str  # effective status (stopped if the tmux session is gone)
    status_text: str
    intake: bool
    repos: list[RepoRow] = field(default_factory=list)

    @property
    def all_merged(self) -> bool:
        """Every repo with work in it is merged (repos left untouched don't hold up archiving)."""
        worked = [r for r in self.repos if not r.untouched]
        return bool(worked) and all(r.merged for r in worked)

    @property
    def base_ci(self) -> str | None:
        """Once every PR is merged: worst CI state of the merge commits (fail > n/a > pending > pass), None = no CI."""
        if not self.all_merged:
            return None
        states = {r.base_ci for r in self.repos}
        return next((s for s in ("fail", "n/a", "pending", "pass") if s in states), None)

    def to_dict(self) -> dict:
        return {
            "key": self.ws.key, "ticket": self.ws.ticket, "name": self.ws.name, "status": self.status,
            "status_text": self.status_text, "folder": self.ws.folder, "intake": self.intake,
            "repos": [{"repo": r.link.repo, "branch": r.link.branch, "git": r.git, "pr": r.pr, "pr_url": r.pr_url,
                       "prs": [{"number": p.number, "state": p.state, "url": p.url} for p in r.prs]}
                      for r in self.repos],
        }


def _status(ws: db.Workstream, alive: bool, intake: bool) -> tuple[str, str]:
    if ws.status == db.ARCHIVED:
        return db.ARCHIVED, "archived"
    if not alive:
        return db.STOPPED, "stopped"
    status = ws.status if ws.status != db.INTAKE else db.YOUR_TURN
    text = {
        db.NEEDS_YOU: f"waiting: {ws.status_reason or 'input'}",
        db.WORKING: "working",
        db.YOUR_TURN: "your turn",
    }.get(status, status)
    if intake and status != db.NEEDS_YOU:
        text = "intake · " + text
    if ws.drift:
        text += " · ⚠ drift"
    return status, text


BLUE = "dodger_blue1"  # waiting on someone else (CI, reviewers)
PURPLE = "bold medium_purple1"  # merged, as on GitHub; green is for an open PR that can be merged


def git_state(link: db.RepoLink, st: gitops.Status | None, pr: db.PR | None,
              operation: str | None = None, after_pr: int = 0, in_base: bool = False,
              others: Sequence[db.PR] = ()) -> tuple[str, str]:
    """What this repo needs next, as (label, rich style). First match wins: broken, merged, already in base, closed,
    local work (which makes the PR's own checks stale), then the open PR. `pr`: the worktree branch's PR. `after_pr`:
    local commits after a merged PR's head. `in_base`: the branch's changes reached base some other way (see
    gitops.in_base). `others`: PRs from the workstream's other branches (gh.RepoPRs): an open one says what's next
    once the branch's own PR is merged, closed or missing, and a merged one counts as the branch's when the branch
    has no commits of its own."""
    base = link.base.removeprefix("origin/")
    if st is None:
        return "⚠ worktree missing", "bold red"
    if st.branch != link.branch:
        return f"⚠ on {st.branch or 'detached HEAD'}", "bold red"
    if operation:
        return f"⚠ {operation} in progress", "bold red"
    prs = gh.RepoPRs(base, pr, list(others))
    open_pr, last = prs.open(), prs.last_merged()
    state = pr.state if pr else None
    if state == "merged":
        if st.dirty:
            return f"merged · {st.dirty} uncommitted", "yellow"
        if after_pr:
            return f"merged · {after_pr} commit{'s' * (after_pr != 1)} not in PR", "yellow"
        return _pr_state(open_pr, base) if open_pr else _merged(last.checks, base)
    if in_base:
        if st.dirty:
            return f"already in {base} · {st.dirty} uncommitted", "yellow"
        return _pr_state(open_pr, base) if open_pr else (f"✓ already in {base}", PURPLE)
    if open_pr:
        pr, state = open_pr, "open"  # the branch's own PR is closed or missing: the open one is what's next
    elif last and not st.ahead_of_base:  # nothing of its own: the work landed through other branches
        return (f"merged · {st.dirty} uncommitted", "yellow") if st.dirty else _merged(last.checks, base)
    if state == "closed":
        return "PR closed, not merged", "grey50"
    if st.dirty:
        return f"✎ {st.dirty} uncommitted", "yellow"
    if st.unpushed:
        return f"↑ needs push ({st.unpushed})", "yellow"
    if state != "open" and not st.ahead_of_base:
        return "no changes yet", "grey50"
    if state == "none":
        return "needs PR", "cyan"
    if state != "open":  # PR status not fetched yet, or gh failed
        return "pushed · PR ?", "grey50"
    return _pr_state(pr, base)


def _merged(checks: str | None, base: str) -> tuple[str, str]:
    """The work is merged: what the base branch's CI says about it."""
    return {
        "fail": (f"✗ merged · CI failing on {base}", "bold red"),
        "pending": (f"… merged · CI running on {base}", BLUE),
        "n/a": ("merged · CI unknown", "yellow"),
    }.get(checks, ("✓ merged", PURPLE))


def _pr_state(pr: db.PR, base: str) -> tuple[str, str]:
    """What an open PR is waiting for."""
    if pr.is_draft:
        return "draft PR", "grey50"
    if pr.checks == "fail":
        return "✗ CI failing", "bold red"
    if pr.mergeable == "CONFLICTING" or pr.merge_state == "DIRTY":
        return f"⚠ conflicts with {base}", "bold red"
    if pr.review == "CHANGES_REQUESTED":
        return "changes requested", "dark_orange"
    if pr.checks == "pending":
        return "… CI running", BLUE
    if pr.merge_state == "BEHIND":  # branch protection requires the branch to be up to date
        return f"needs update from {base}", "yellow"
    if pr.review == "REVIEW_REQUIRED":
        return "awaiting review", BLUE
    if pr.merge_state == "BLOCKED":
        return "blocked by branch rules", BLUE
    return "● ready to merge", "bold green"


GIT_MAX_AGE = 20  # seconds a repo's git state is reused while no git file changed (see _read_git)
STOPPED_GIT_MAX_AGE = 300  # the same for a stopped workstream: no Claude there to edit files


@dataclass
class _GitRead:
    stamp: tuple
    at: float
    st: gitops.Status | None
    operation: str | None
    after_pr: int
    in_base: bool


_git_reads: dict[str, _GitRead] = {}  # worktree path -> its last read, for the dashboards' load() every few seconds


def _read_git(ws: db.Workstream, link: db.RepoLink, pr: db.PR | None,
              max_age: float) -> tuple[gitops.Status | None, str | None, int, bool]:
    """(status, operation in progress, commits after the merged PR, in base) for one repo: a few git processes, so
    the last read is reused for up to `max_age` seconds while the worktree's git files (gitops.fingerprint), its PR
    and its workstream's status are the same. Commits, checkouts, staging, fetches and rebases change those files and
    show at once; edits to files in the worktree show within `max_age`, or when Claude stops (status changes)."""
    wt = Path(link.worktree_path)
    if not wt.is_dir():
        return None, None, 0, False
    pr_merged = bool(pr and pr.state == "merged")
    refs = [f"refs/heads/{link.branch}", f"refs/remotes/origin/{link.branch}",
            f"refs/remotes/{link.base}", f"refs/heads/{link.base}"]

    def stamp() -> tuple | None:
        fp = gitops.fingerprint(wt, refs)
        return fp and (fp, link.branch, link.base, pr_merged, pr.head_sha if pr else None, ws.status, ws.status_at)

    last = _git_reads.get(link.worktree_path)
    now = time.time()
    if last and now - last.at < max_age and last.stamp == stamp():
        return last.st, last.operation, last.after_pr, last.in_base
    try:
        st = gitops.status(wt, link.base)
        operation = gitops.operation_in_progress(wt)
        after_pr = gitops.count_commits(wt, f"{pr.head_sha}..HEAD") if pr_merged and pr.head_sha else 0
        in_base = not pr_merged and st.ahead_of_base > 0 and gitops.in_base(wt, link.base)
    except gitops.GitError:
        st, operation, after_pr, in_base = None, None, 0, False
    # stamped after reading: `git status` may rewrite the index itself (refreshing file times), which mustn't count
    # as a change; a change made during the read shows within max_age
    if after := stamp():
        _git_reads[link.worktree_path] = _GitRead(after, now, st, operation, after_pr, in_base)
    return st, operation, after_pr, in_base


def load(include_archived: bool = False, git_status: bool = True, git_max_age: float = 0) -> list[Row]:
    """`git_max_age`: for the dashboards, how long a repo's git state may be reused (see _read_git); 0 reads it all.
    Stopped workstreams get STOPPED_GIT_MAX_AGE instead."""
    conn = db.connect()
    live = tmux.sessions()
    fetches = db.get_fetches(conn)
    rows = []
    for ws in db.list_workstreams(conn, include_archived=include_archived):
        links = db.list_repos(conn, ws.key)
        prs, branch_prs = db.get_prs(conn, ws.key), db.get_branch_prs(conn, ws.key)
        intake = not (ws.name and links and (ws.path / templates.TASK_FILE).exists())
        status, text = _status(ws, ws.key in live, intake)
        row = Row(ws, status, text, intake)
        max_age = max(git_max_age, STOPPED_GIT_MAX_AGE) if git_max_age and status == db.STOPPED else git_max_age
        for link in links:
            rp = gh.repo_prs(link, prs.get(link.repo), branch_prs.get(link.repo, []))
            pr = rp.own
            merged, state, style, behind, untouched = rp.landed(None, False), "", "", 0, False
            if git_status and ws.status != db.ARCHIVED:
                st, operation, after_pr, in_base = _read_git(ws, link, pr, max_age)
                state, style = git_state(link, st, pr, operation, after_pr, in_base, rp.others)
                merged = rp.landed(st.ahead_of_base if st else None, in_base)
                if st:
                    behind = 0 if merged else st.behind_base
                    untouched = not (st.dirty or st.ahead_of_base) and not rp.active
            f = fetches.get(link.repo_path)
            lead, last = rp.lead(), rp.last_merged()
            row.repos.append(RepoRow(link, state, rp.describe(), lead.url if lead else None, merged,
                                     last.checks if last else None, behind, style, untouched,
                                     f["ok_at"] if f else None, f["error"] if f else None, rp.shown()))
        rows.append(row)
    rows.sort(key=lambda r: (SORT.get(r.status, 9), -r.ws.status_at))
    return rows
