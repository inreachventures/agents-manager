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
    monkeypatch.setattr(gh, "fetch_pr", lambda *a: {**PR, "url": "u"})
    runs = {"check-runs": {"check_runs": [{"status": "completed", "conclusion": "failure"}]}}
    calls = fake_gh(monkeypatch, runs)

    gh.refresh(conn, "WS-1")
    assert db.get_prs(conn, "WS-1")["r"].checks == "fail"
    runs["check-runs"] = {"check_runs": [{"status": "completed", "conclusion": "success"}]}  # re-run went green
    gh.refresh(conn, "WS-1")
    assert db.get_prs(conn, "WS-1")["r"].checks == "pass" and len(calls) == 2
    gh.refresh(conn, "WS-1")  # pass is final: not asked again
    assert db.get_prs(conn, "WS-1")["r"].checks == "pass" and len(calls) == 2
