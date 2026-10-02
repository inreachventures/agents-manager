import sqlite3

import pytest

from agentmgr import db, gitops, view
from agentmgr import workstream as ws_mod
from agentmgr.workstream import WorkstreamError

from .conftest import sh

LINK = db.RepoLink("WS-1", "r", "/r", "/w", "ws-1-x", "origin/main", True, 0)


def st(dirty=0, ahead=1, behind=0, unpushed=0, branch="ws-1-x"):
    return gitops.Status(branch, dirty, ahead, behind, "origin/ws-1-x", unpushed)


def pr(state="open", checks=None, draft=False, review=None, mergeable=None, merge_state=None):
    return db.PR("WS-1", "r", 5, state, draft, review, checks, None, 0, mergeable, merge_state, "abc")


@pytest.mark.parametrize(("status", "pull", "kw", "label", "style"), [
    (None, None, {}, "⚠ worktree missing", "bold red"),
    (st(branch="main"), pr(), {}, "⚠ on main", "bold red"),
    (st(branch=None), pr(), {}, "⚠ on detached HEAD", "bold red"),
    (st(), pr(), {"operation": "rebase"}, "⚠ rebase in progress", "bold red"),
    # merged: behind/unpushed counts are irrelevant (squash merges leave both behind)
    (st(dirty=2, unpushed=3), pr("merged", "pass"), {}, "merged · 2 uncommitted", "yellow"),
    (st(unpushed=3), pr("merged", "pass"), {"after_pr": 1}, "merged · 1 commit not in PR", "yellow"),
    (st(), pr("merged", "fail"), {}, "✗ merged · CI failing on main", "bold red"),
    (st(), pr("merged", "pending"), {}, "… merged · CI running on main", view.BLUE),
    (st(), pr("merged", "n/a"), {}, "merged · CI unknown", "yellow"),
    (st(unpushed=3, behind=4), pr("merged", "pass"), {}, "✓ merged", "bold green"),
    (st(), pr("merged", None), {}, "✓ merged", "bold green"),
    (st(), pr("closed"), {}, "PR closed, not merged", "grey50"),
    # local work beats the open PR's state
    (st(dirty=3), pr(checks="fail"), {}, "✎ 3 uncommitted", "yellow"),
    (st(unpushed=2), pr(checks="pass"), {}, "↑ needs push (2)", "yellow"),
    (st(ahead=0), pr("none"), {}, "no changes yet", "grey50"),
    (st(ahead=0), None, {}, "no changes yet", "grey50"),
    (st(), pr("none"), {}, "needs PR", "cyan"),
    (st(), pr("n/a"), {}, "pushed · PR ?", "grey50"),
    (st(), pr(draft=True, checks="fail"), {}, "draft PR", "grey50"),
    (st(), pr(checks="fail", review="APPROVED"), {}, "✗ CI failing", "bold red"),
    (st(), pr(checks="pass", mergeable="CONFLICTING"), {}, "⚠ conflicts with main", "bold red"),
    (st(), pr(checks="pass", review="CHANGES_REQUESTED"), {}, "changes requested", "dark_orange"),
    (st(), pr(checks="pending"), {}, "… CI running", view.BLUE),
    (st(), pr(checks="pass", merge_state="BEHIND"), {}, "needs update from main", "yellow"),
    (st(), pr(checks="pass", review="REVIEW_REQUIRED", merge_state="BLOCKED"), {}, "awaiting review", view.BLUE),
    (st(), pr(checks="pass", merge_state="BLOCKED"), {}, "blocked by branch rules", view.BLUE),
    (st(), pr(checks="pass", review="APPROVED", merge_state="CLEAN"), {}, "● ready to merge", "bold medium_purple1"),
    (st(), pr(checks=None, merge_state="UNKNOWN"), {}, "● ready to merge", "bold medium_purple1"),
])
def test_git_state(status, pull, kw, label, style):
    assert view.git_state(LINK, status, pull, **kw) == (label, style)


def _row(ws_key):
    [row] = [r for r in view.load() if r.ws.key == ws_key]
    return row


def _set_pr(ws_key, repo, state, head_sha=None):
    db.upsert_pr(db.connect(), db.PR(ws_key, repo, 5, state, False, None, None, None, 0, head_sha=head_sha))


def test_lifecycle_and_squash_merge(env, monkeypatch):
    monkeypatch.setattr("agentmgr.gh.refresh", lambda *a, **k: None)  # PR state is set by hand below
    ws = ws_mod.new(launch=False, ticket="PROJ-3", name="squash me", repo_queries=["web", "release"])
    wt = ws.path / "acme-web"
    row = _row(ws.key)
    assert [r.git for r in row.repos] == ["no changes yet", "no changes yet"]
    assert not row.all_merged

    (wt / "f.txt").write_text("x")
    assert _row(ws.key).repos[0].git == "✎ 1 uncommitted"
    sh(wt, "git", "add", ".")
    sh(wt, "git", "commit", "-q", "-m", "PROJ-3 work")
    assert _row(ws.key).repos[0].git == "↑ needs push (1)"
    sh(wt, "git", "push", "-q", "origin", "HEAD")  # without -u: the remote branch still counts as pushed
    assert _row(ws.key).repos[0].git == "pushed · PR ?"  # PR status not fetched yet
    _set_pr(ws.key, "acme-web", "none")
    assert _row(ws.key).repos[0].git == "needs PR"
    head = sh(wt, "git", "rev-parse", "HEAD")

    # squash-merge on origin: main gets a new commit, the PR branch is deleted
    other = env.root / "other"
    sh(env.root, "git", "clone", "-q", str(env.origins / "acme-web.git"), str(other))
    (other / "f.txt").write_text("x")
    sh(other, "git", "add", ".")
    sh(other, "git", "commit", "-q", "-m", "PROJ-3 work (#5)")
    sh(other, "git", "push", "-q", "origin", "HEAD:main", ":proj-3-squash-me")
    sh(wt, "git", "fetch", "-q", "--prune", "origin")
    # not known to be merged yet, and the remote branch is gone: the commit only exists locally
    assert _row(ws.key).repos[0].git == "↑ needs push (1) · 1 behind base"

    _set_pr(ws.key, "acme-web", "merged", head)
    row = _row(ws.key)
    assert row.repos[0].git == "✓ merged" and row.repos[0].behind == 0
    assert row.all_merged  # the untouched release-tools repo doesn't hold it up

    # a commit after the merge is flagged, and blocks archiving
    (wt / "g.txt").write_text("y")
    sh(wt, "git", "add", ".")
    sh(wt, "git", "commit", "-q", "-m", "PROJ-3 follow-up")
    assert _row(ws.key).repos[0].git == "merged · 1 commit not in PR"
    with pytest.raises(WorkstreamError, match="1 commit\\(s\\) made after PR #5 was merged"):
        ws_mod.archive(ws.key, delete_branches=True)
    assert gitops.branch_exists(env.code / "acme-web", "proj-3-squash-me")


def test_old_database_gets_new_pr_columns(env):
    path = env.root / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE prs (ws_key TEXT NOT NULL, repo TEXT NOT NULL, number INTEGER, state TEXT, "
                "is_draft INTEGER, review TEXT, checks TEXT, url TEXT, fetched_at REAL NOT NULL, "
                "PRIMARY KEY (ws_key, repo))")
    old.execute("INSERT INTO prs VALUES ('WS-1', 'r', 5, 'open', 0, NULL, 'pass', NULL, 0)")
    old.commit()
    old.close()
    conn = db.connect(path)
    assert db.get_prs(conn, "WS-1")["r"].merge_state is None
    db.upsert_pr(conn, pr(merge_state="CLEAN"))
    assert db.get_prs(conn, "WS-1")["r"].merge_state == "CLEAN"
    db.connect(path)  # idempotent
