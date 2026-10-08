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
    # the branch's changes reached main some other way, e.g. the last PR of a stack, opened from another branch
    (st(unpushed=3, behind=4), pr("closed"), {"in_base": True}, "✓ already in main", "bold green"),
    (st(dirty=2), None, {"in_base": True}, "already in main · 2 uncommitted", "yellow"),
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
    # the PR isn't known to be merged yet, but git already sees its changes in main
    assert _row(ws.key).repos[0].git == "✓ already in main"

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


def test_landed_from_another_branch(env, monkeypatch):
    """A stack: the workstream branch's own PR is closed, a later PR from another branch brings everything to main."""
    monkeypatch.setattr("agentmgr.gh.refresh", lambda *a, **k: None)
    ws = ws_mod.new(launch=False, ticket="PROJ-6", name="stack me", repo_queries=["web"])
    wt = ws.path / "acme-web"
    for name in ("f.txt", "g.txt"):
        (wt / name).write_text(name)
        sh(wt, "git", "add", ".")
        sh(wt, "git", "commit", "-q", "-m", f"PROJ-6 {name}")
    sh(wt, "git", "push", "-q", "origin", "HEAD", "HEAD:proj-6-stack-top")
    sh(wt, "git", "commit", "-q", "--allow-empty", "-m", "PROJ-6 local only")
    _set_pr(ws.key, "acme-web", "closed", sh(wt, "git", "rev-parse", "HEAD~"))
    assert _row(ws.key).repos[0].git == "PR closed, not merged"
    assert ws_mod.rebase_plan(ws.key)[1][0].skip == "up to date with origin/main"

    # main moves on, then the top of the stack is squash-merged
    other = env.root / "other"
    sh(env.root, "git", "clone", "-q", str(env.origins / "acme-web.git"), str(other))
    (other / "h.txt").write_text("h")
    sh(other, "git", "add", ".")
    sh(other, "git", "commit", "-q", "-m", "someone else")
    sh(other, "git", "merge", "-q", "--squash", "origin/proj-6-stack-top")
    sh(other, "git", "commit", "-q", "-m", "PROJ-6 stack top (#7)")
    sh(other, "git", "push", "-q", "origin", "HEAD:main")
    sh(wt, "git", "fetch", "-q", "origin")

    row = _row(ws.key)
    assert row.repos[0].git == "✓ already in main" and row.repos[0].behind == 0
    assert row.all_merged and row.base_ci is None  # the closed PR's own checks aren't the base branch's CI
    assert ws_mod.rebase_plan(ws.key)[1][0].skip == "its changes are already in origin/main"
    _, [report] = ws_mod.cleanup_report(ws.key, refresh_prs=False)
    assert report.merged and not report.blockers  # local-only commits and the closed PR's head don't block
    ws_mod.archive(ws.key)
    assert not gitops.branch_exists(env.code / "acme-web", "proj-6-stack-me")


def test_conflicting_branch_is_not_in_base(env):
    ws = ws_mod.new(launch=False, ticket="PROJ-8", name="clash", repo_queries=["web"])
    wt = ws.path / "acme-web"
    (wt / "README.md").write_text("mine")
    sh(wt, "git", "commit", "-q", "-am", "PROJ-8 mine")
    other = env.root / "other"
    sh(env.root, "git", "clone", "-q", str(env.origins / "acme-web.git"), str(other))
    (other / "README.md").write_text("theirs")
    sh(other, "git", "commit", "-q", "-am", "theirs")
    sh(other, "git", "push", "-q", "origin", "HEAD:main")
    sh(wt, "git", "fetch", "-q", "origin")
    assert not gitops.in_base(wt, "origin/main")
    sh(wt, "git", "reset", "-q", "--hard", "origin/main")
    assert gitops.in_base(wt, "origin/main")  # nothing of its own: trivially in base


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
