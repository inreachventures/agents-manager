"""PR status via the GitHub CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from . import db

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


def fetch_pr(worktree: Path, branch: str) -> dict | None:
    """Most relevant PR for `branch`: open first, else the most recently updated."""
    proc = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--state", "all", "--json", FIELDS, "--limit", "10"],
        cwd=worktree, capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "gh failed")
    prs = json.loads(proc.stdout or "[]")
    if not prs:
        return None
    prs.sort(key=lambda p: (p["state"] == "OPEN", p.get("updatedAt", "")), reverse=True)
    return prs[0]


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


def refresh(conn, ws_key: str) -> None:
    now = time.time()
    previous = db.get_prs(conn, ws_key)
    for link in db.list_repos(conn, ws_key):
        checks = None
        try:
            pr = fetch_pr(Path(link.worktree_path), link.branch) if available() else None
            state = "none" if pr is None else pr["state"].lower()
            if state == "merged":
                old = previous.get(link.repo)
                if old and old.state == "merged" and old.number == pr["number"] and old.checks in ("pass", "fail"):
                    checks = old.checks  # base-branch CI already finished; don't ask again
                else:
                    try:
                        checks = merge_commit_checks(Path(link.worktree_path), pr)
                    except Exception:  # noqa: BLE001 — keep the merged state even if CI can't be read
                        checks = "n/a"
            elif pr:
                checks = _checks(pr.get("statusCheckRollup"))
        except Exception:  # noqa: BLE001 — never let gh problems break the dashboard
            pr, state = None, "n/a"
        db.upsert_pr(conn, db.PR(
            ws_key=ws_key, repo=link.repo,
            number=pr["number"] if pr else None,
            state=state,
            is_draft=bool(pr and pr.get("isDraft")),
            review=(pr.get("reviewDecision") or None) if pr else None,
            checks=checks,  # open PR: its own checks; merged PR: CI of the merge commit on the base branch
            url=pr["url"] if pr else None,
            fetched_at=now,
            mergeable=pr.get("mergeable") if pr else None,
            merge_state=pr.get("mergeStateStatus") if pr else None,
            head_sha=pr.get("headRefOid") if pr else None,
        ))


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
