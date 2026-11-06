import json
import subprocess
import time

from agentmgr import db, gh, view


def fake_gh(monkeypatch, responses):
    """Answer `gh api <path>` from `responses` (path substring -> payload)."""
    calls = []

    def run(args, **kw):
        calls.append(args)
        path = args[2]
        payload = next(v for k, v in responses.items() if k in path)
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    monkeypatch.setattr("agentmgr.gh.subprocess.run", run)
    return calls


PR = {"number": 5, "state": "MERGED", "mergeCommit": {"oid": "abc"}, "mergedAt": "2020-01-01T00:00:00Z"}


def test_merge_commit_checks(monkeypatch):
    fake_gh(monkeypatch, {"check-runs": {"check_runs": [
        {"status": "completed", "conclusion": "success"}, {"status": "completed", "conclusion": "skipped"}]}})
    assert gh.merge_commit_checks(".", PR) == "pass"

    fake_gh(monkeypatch, {"check-runs": {"check_runs": [
        {"status": "completed", "conclusion": "success"}, {"status": "in_progress", "conclusion": None}]}})
    assert gh.merge_commit_checks(".", PR) == "pending"

    fake_gh(monkeypatch, {"check-runs": {"check_runs": [{"status": "completed", "conclusion": "failure"}]}})
    assert gh.merge_commit_checks(".", PR) == "fail"


def test_merge_commit_checks_statuses_and_grace(monkeypatch):
    # statuses are only used without check runs
    calls = fake_gh(monkeypatch, {"check-runs": {"check_runs": []}, "/status": {"statuses": [{"state": "success"}]}})
    assert gh.merge_commit_checks(".", PR) == "pass" and len(calls) == 2
    # no CI at all: None, unless the merge just happened and checks may not exist yet
    fake_gh(monkeypatch, {"check-runs": {"check_runs": []}, "/status": {"statuses": []}})
    assert gh.merge_commit_checks(".", PR) is None
    just_now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    assert gh.merge_commit_checks(".", {**PR, "mergedAt": just_now}) == "pending"


def pr(checks):
    return db.PR("WS-1", "r", 5, "merged", False, None, checks, None, 0)


def test_describe_and_row_state():
    assert gh.describe(pr("pass")) == "PR #5 merged · CI ✓"
    assert gh.describe(pr(None)) == "PR #5 merged"

    def row(*states):
        link = db.RepoLink("WS-1", "r", "/r", "/w", "b", "origin/main", True, 0)
        r = view.Row(db.Workstream("WS-1", None, None, "s", "/w", "stopped", None, 0, 0, None), "stopped", "", False)
        r.repos = [view.RepoRow(link, "", "", None, True, s) for s in states]
        return r

    assert row("pass", "pass").base_ci == "pass"
    assert row("pass", "pending").base_ci == "pending"
    assert row("pending", "fail").base_ci == "fail"
    assert row(None).base_ci is None


def test_refresh_rechecks_failed_base_ci(env, monkeypatch):
    """A failed merge-commit run can be re-run green: only a pass is final."""
    conn = db.connect()
    db.insert_workstream(conn, db.Workstream("WS-1", None, None, "s", "/w", "stopped", None, 0, 0, None))
    db.insert_repo(conn, db.RepoLink("WS-1", "r", "/r", ".", "b", "origin/main", True, 0))
    monkeypatch.setattr(gh, "available", lambda: True)
    monkeypatch.setattr(gh, "fetch_prs", lambda *a: [{**PR, "url": "u", "headRefName": "b"}])
    runs = {"check-runs": {"check_runs": [{"status": "completed", "conclusion": "failure"}]}}
    calls = fake_gh(monkeypatch, runs)

    gh.refresh(conn, "WS-1")
    assert db.get_prs(conn, "WS-1")["r"].checks == "fail"
    runs["check-runs"] = {"check_runs": [{"status": "completed", "conclusion": "success"}]}  # re-run went green
    gh.refresh(conn, "WS-1")
    assert db.get_prs(conn, "WS-1")["r"].checks == "pass" and len(calls) == 2
    gh.refresh(conn, "WS-1")  # pass is final: not asked again
    assert db.get_prs(conn, "WS-1")["r"].checks == "pass" and len(calls) == 2


def test_fetch_prs(monkeypatch):
    """One search per prefix finds the branch's own PRs and those of the workstream's other branches; a branch whose
    name lacks the prefix is looked up by name."""
    calls = []
    prs = [{"number": 1, "headRefName": "ws-1-x"}, {"number": 2, "headRefName": "ws-1-stack-top"},
           {"number": 3, "headRefName": "WS-1-Upper"}, {"number": 4, "headRefName": "ws-12-other"}]

    def run(args, **kw):
        calls.append(args)
        if "--head" in args:
            out = [{"number": 9, "headRefName": "custom"}]
        else:
            out = prs
        return subprocess.CompletedProcess(args, 0, json.dumps(out), "")

    monkeypatch.setattr("agentmgr.gh.subprocess.run", run)
    assert sorted(p["number"] for p in gh.fetch_prs(".", "ws-1-x", ["ws-1-"])) == [1, 2, 3]
    assert len(calls) == 1 and calls[0][calls[0].index("--search") + 1] == "head:ws-1-"
    calls.clear()
    assert sorted(p["number"] for p in gh.fetch_prs(".", "custom", ["ws-1-"])) == [1, 2, 3, 9]
    assert len(calls) == 2


def _pr(number, head, state, merged_at=None, base="main", **kw):
    return {"number": number, "headRefName": head, "baseRefName": base, "state": state, "url": f"u{number}",
            "mergedAt": merged_at, "mergeCommit": {"oid": f"c{number}"} if merged_at else None, **kw}


def test_refresh_keeps_the_workstreams_other_prs(env, monkeypatch):
    conn = db.connect()
    for key in ("WS-1", "WS-2"):
        db.insert_workstream(conn, db.Workstream(key, None, None, f"s{key}", "/w", "stopped", None, 0, 0, None))
    db.insert_repo(conn, db.RepoLink("WS-1", "r", "/r", ".", "ws-1-x", "origin/main", True, 0))
    db.insert_repo(conn, db.RepoLink("WS-2", "r", "/r", ".", "ws-1-taken-over", "origin/main", True, 0))
    monkeypatch.setattr(gh, "available", lambda: True)
    found = [_pr(10, "ws-1-x", "CLOSED"),
             _pr(11, "ws-1-a", "MERGED", "2026-01-02T00:00:00Z"),
             _pr(12, "ws-1-b", "MERGED", "2026-01-01T00:00:00Z"),  # created later, merged earlier
             _pr(13, "ws-1-c", "OPEN", statusCheckRollup=[{"conclusion": "SUCCESS"}]),
             _pr(14, "ws-1-taken-over", "OPEN")]  # WS-2's branch: its PR, not WS-1's
    monkeypatch.setattr(gh, "fetch_prs", lambda wt, branch, prefixes: found)
    calls = fake_gh(monkeypatch, {"check-runs": {"check_runs": [{"conclusion": "success"}]}})

    gh.refresh(conn, "WS-1")
    assert db.get_prs(conn, "WS-1")["r"].number == 10
    rows = db.get_branch_prs(conn, "WS-1")["r"]
    assert [(p.number, p.state, p.checks) for p in rows] == [
        (10, "closed", None), (11, "merged", "pass"), (12, "merged", None), (13, "open", "pass")]
    assert len(calls) == 1 and "c11" in calls[0][2]  # only the last merge's CI is the base branch's

    link = db.list_repos(conn, "WS-1")[0]
    rp = gh.repo_prs(link, db.get_prs(conn, "WS-1")["r"], rows)
    assert rp.describe() == "PRs #11 merged · CI ✓, #12 merged, #13 open ✓"
    assert rp.open().number == 13 and rp.lead().url == "u13" and rp.last_merged().number == 11
    assert not rp.landed(0, False)  # #13 is still open

    found[3] = _pr(13, "ws-1-c", "CLOSED")
    gh.refresh(conn, "WS-1")
    rp = gh.repo_prs(link, db.get_prs(conn, "WS-1")["r"], db.get_branch_prs(conn, "WS-1")["r"])
    assert rp.describe() == "PRs #11 merged · CI ✓, #12 merged" and rp.lead().number == 11
    assert len(calls) == 1  # the pass is final
    assert rp.landed(0, False) and not rp.landed(2, False) and rp.landed(2, True)

    def broken(*a):
        raise RuntimeError("gh: not logged in")

    monkeypatch.setattr(gh, "fetch_prs", broken)
    gh.refresh(conn, "WS-1")
    assert db.get_prs(conn, "WS-1")["r"].state == "n/a" and "r" not in db.get_branch_prs(conn, "WS-1")


def test_repo_prs_without_other_branches():
    link = db.RepoLink("WS-1", "r", "/r", "/w", "b", "origin/main", True, 0)
    own = db.PR("WS-1", "r", 5, "open", False, "APPROVED", "pass", "u5", 0)
    rp = gh.repo_prs(link, own, [])
    assert rp.describe() == "PR #5 open ✓ approved" and rp.lead() is own and rp.open() is own
    assert gh.repo_prs(link, None, []).describe() == "PR ?"
    none = db.PR("WS-1", "r", None, "none", False, None, None, None, 0)
    assert gh.repo_prs(link, none, []).describe() == "no PR" and gh.repo_prs(link, none, []).lead() is None
    # only closed PRs from other branches: listed all the same
    closed = db.PR("WS-1", "r", 7, "closed", False, None, None, "u7", 0, head="ws-1-y")
    assert gh.repo_prs(link, none, [closed]).describe() == "PR #7 closed"
