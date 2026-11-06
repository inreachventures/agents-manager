"""PR status via the GitHub CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import db, naming

FIELDS = ("number,state,isDraft,reviewDecision,statusCheckRollup,url,updatedAt,mergeCommit,mergedAt,"
          "mergeable,mergeStateStatus,headRefOid")
CI_START_GRACE = 600  # seconds after a merge during which "no checks yet" means CI hasn't started


def available() -> bool:
    return shutil.which("gh") is not None


def _checks(rollup: list[dict] | None) -> str | None:
    """Collapse statusCheckRollup into pass / fail / pending / None."""
    if not rollup:
        return None
    states = set()
    for c in rollup:
        s = (c.get("conclusion") or c.get("state") or c.get("status") or "").upper()
        if s in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"):
            states.add("fail")
        elif s in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            states.add("pass")
        else:
            states.add("pending")
    for s in ("fail", "pending", "pass"):
        if s in states:
            return s
    return None


def _pr_list(worktree: Path, *args: str) -> list[dict]:
    proc = subprocess.run(
        ["gh", "pr", "list", "--state", "all", "--json", f"{FIELDS},headRefName,baseRefName", *args],
        cwd=worktree, capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "gh failed")
    return json.loads(proc.stdout or "[]")


def fetch_prs(worktree: Path, branch: str, prefixes: list[str]) -> list[dict]:
    """PRs opened from `branch`, or from any other branch whose name starts with one of the workstream's `prefixes`
    (naming.branch_prefixes, e.g. 'proj-313-'): stacks and re-cut branches. One search per prefix, which also finds
    the branch's own PRs when its name carries the prefix (the usual case); a separate lookup otherwise."""
    found = {}
    if not any(branch.lower().startswith(p) for p in prefixes):
        found |= {p["number"]: p for p in _pr_list(worktree, "--head", branch, "--limit", "10")}
    for prefix in prefixes:
        # GitHub's head: qualifier matches branch names by prefix; keep only exact prefix matches all the same
        for p in _pr_list(worktree, "--search", f"head:{prefix}", "--limit", "50"):
            if p.get("headRefName", "").lower().startswith(prefix):
                found[p["number"]] = p
    return list(found.values())


def _gh_api(worktree: Path, path: str) -> dict:
    proc = subprocess.run(["gh", "api", path], cwd=worktree, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "gh api failed")
    return json.loads(proc.stdout or "{}")


def _parse_time(value: str | None) -> float | None:
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None
    except ValueError:
        return None


def merge_commit_checks(worktree: Path, pr: dict) -> str | None:
    """CI of a merged PR's merge commit on the base branch: pass / fail / pending / None (no CI)."""
    sha = (pr.get("mergeCommit") or {}).get("oid")
    if not sha:
        return None
    runs = _gh_api(worktree, f"repos/{{owner}}/{{repo}}/commits/{sha}/check-runs?per_page=100").get("check_runs", [])
    if not runs:  # legacy commit statuses only when there are no check runs: bots (e.g. license/cla) often
        # leave a status on merge commits "pending" forever
        runs = _gh_api(worktree, f"repos/{{owner}}/{{repo}}/commits/{sha}/status").get("statuses", [])
    result = _checks(runs)
    merged_at = _parse_time(pr.get("mergedAt"))
    if result is None and merged_at and time.time() - merged_at < CI_START_GRACE:
        return "pending"  # just merged: checks may not have been created yet
    return result


def _pr_checks(worktree: Path, pr: dict, last_merged: bool, old: db.PR | None) -> str | None:
    """An open or closed PR: its own checks. The repo's last merged PR: CI of its merge commit on the base branch,
    where the work landed; a pass is final (a failed run can be re-run green). Earlier merged PRs: None."""
    if pr["state"] != "MERGED":
        return _checks(pr.get("statusCheckRollup"))
    if not last_merged:
        return None
    if old and old.state == "merged" and old.checks == "pass":
        return "pass"
    try:
        return merge_commit_checks(worktree, pr)
    except Exception:  # noqa: BLE001 — keep the merged state even if CI can't be read
        return "n/a"


def _pr(ws_key: str, repo: str, pr: dict, checks: str | None, now: float) -> db.PR:
    return db.PR(
        ws_key=ws_key, repo=repo, number=pr["number"], state=pr["state"].lower(), is_draft=bool(pr.get("isDraft")),
        review=pr.get("reviewDecision") or None, checks=checks, url=pr.get("url"), fetched_at=now,
        mergeable=pr.get("mergeable"), merge_state=pr.get("mergeStateStatus"), head_sha=pr.get("headRefOid"),
        head=pr.get("headRefName"), base=pr.get("baseRefName"), merged_at=_parse_time(pr.get("mergedAt")),
    )


def refresh(conn, ws_key: str) -> None:
    """Re-read each repo's PRs: the worktree branch's (prs: its open one, else the most recently updated, or "none"
    or "n/a") and, in branch_prs, that one and those from the workstream's other branches. A branch another
    workstream has in the same repo is that workstream's, even if its name carries this one's prefix."""
    now = time.time()
    ws = db.get_workstream(conn, ws_key)
    prefixes = naming.branch_prefixes(ws.key, ws.ticket) if ws else []
    own_before, all_before = db.get_prs(conn, ws_key), db.get_branch_prs(conn, ws_key)
    for link in db.list_repos(conn, ws_key):
        wt = Path(link.worktree_path)
        before = [own_before.get(link.repo), *all_before.get(link.repo, [])]
        previous = {p.number: p for p in before if p and p.number is not None}
        try:
            found = fetch_prs(wt, link.branch, prefixes) if available() else []
            skip = {link.branch, *db.other_branches(conn, ws_key, link.repo_path)}
            own = max((p for p in found if p.get("headRefName") == link.branch),
                      key=lambda p: (p["state"] == "OPEN", p.get("updatedAt", "")), default=None)
            kept = sorted([*([own] if own else []), *(p for p in found if p.get("headRefName") not in skip)],
                          key=lambda p: p["number"])
            last = max((p for p in kept if p["state"] == "MERGED"), key=lambda p: p.get("mergedAt") or "",
                       default=None)
            rows = [_pr(ws_key, link.repo, p, _pr_checks(wt, p, p is last, previous.get(p["number"])), now)
                    for p in kept]
            own_row = next((r for r in rows if own and r.number == own["number"]), None)
            own_row = own_row or db.PR(ws_key, link.repo, None, "none", False, None, None, None, now)
        except Exception:  # noqa: BLE001 — never let gh problems break the dashboard
            rows, own_row = [], db.PR(ws_key, link.repo, None, "n/a", False, None, None, None, now)
        db.upsert_pr(conn, own_row)
        db.set_branch_prs(conn, ws_key, link.repo, rows)


@dataclass
class RepoPRs:
    """A repo's PRs in a workstream: `own`, from the worktree branch (its prs row, which also says "none" or "n/a"),
    and `others`, from the workstream's other branches (a stack, a re-cut branch)."""
    base: str  # the repo's base branch, without origin/
    own: db.PR | None
    others: list[db.PR] = field(default_factory=list)

    @property
    def all(self) -> list[db.PR]:
        own = [self.own] if self.own and self.own.number is not None and self.own.state not in ("none", "n/a") else []
        return sorted(own + self.others, key=lambda p: p.number)

    def open(self) -> db.PR | None:
        """The open PR that says what the repo needs next: the worktree branch's, else the one into the base branch
        (the bottom of a stack), else the oldest."""
        if self.own and self.own.state == "open":
            return self.own
        return min((p for p in self.others if p.state == "open"), key=lambda p: (p.base != self.base, p.number),
                   default=None)

    def last_merged(self) -> db.PR | None:
        """The PR whose merge put the work's latest state on base: its checks are the base branch's CI."""
        return max((p for p in self.all if p.state == "merged"), key=lambda p: (p.merged_at or 0, p.number),
                   default=None)

    def lead(self) -> db.PR | None:
        """The PR to open from the dashboard: the open one, else the last merged, else the newest."""
        return self.open() or self.last_merged() or (self.all[-1] if self.all else None)

    @property
    def active(self) -> bool:
        return any(p.state in ("open", "merged") for p in self.all)

    def landed(self, ahead_of_base: int | None, in_base: bool) -> bool:
        """The repo's work reached base: no PR still open, and the worktree branch's PR merged, or its changes are in
        base anyway (gitops.in_base), or it has no commits of its own (`ahead_of_base`, None if unknown) and a PR from
        another branch merged."""
        if any(p.state == "open" for p in self.all):
            return False
        if (self.own and self.own.state == "merged") or in_base:
            return True
        return ahead_of_base == 0 and any(p.state == "merged" for p in self.others)

    def shown(self) -> list[db.PR]:
        """The PRs worth listing: all but the closed ones, unless every one is closed."""
        return [p for p in self.all if p.state != "closed"] or self.all

    def describe(self) -> str:
        """The PR column: 'PR #412 open ✓' as before, or 'PRs #415 merged, #418 open ✓' with PRs from other branches."""
        if not self.others or (self.own and self.own.state == "n/a"):
            return describe(self.own)
        shown = self.shown()
        if len(shown) == 1:
            return describe(shown[0])
        return "PRs " + ", ".join(describe(p).removeprefix("PR ") for p in shown)


def repo_prs(link: db.RepoLink, own: db.PR | None, rows: list[db.PR]) -> RepoPRs:
    """`own`: the repo's prs row; `rows`: its branch_prs rows."""
    if own and own.number is not None:
        if same := next((p for p in rows if p.number == own.number), None):
            own = replace(own, head=same.head, base=same.base, merged_at=same.merged_at)
    return RepoPRs(link.base.removeprefix("origin/"), own, [p for p in rows if p.head != link.branch])


def describe(pr: db.PR | None) -> str:
    if pr is None:
        return "PR ?"
    if pr.state == "n/a":
        return "PR n/a"
    if pr.state == "none" or pr.number is None:
        return "no PR"
    label = "draft" if pr.is_draft and pr.state == "open" else pr.state
    extra = []
    if pr.state == "merged" and pr.checks:
        extra.append({"pass": "· CI ✓", "fail": "· CI ✗", "pending": "· CI …", "n/a": "· CI ?"}.get(pr.checks, ""))
    if pr.state == "open":
        extra.append({"pass": "✓", "fail": "✗", "pending": "…"}.get(pr.checks or "", ""))
        if pr.review == "APPROVED":
            extra.append("approved")
        elif pr.review == "CHANGES_REQUESTED":
            extra.append("changes requested")
    return " ".join(p for p in [f"PR #{pr.number} {label}", *extra] if p)
