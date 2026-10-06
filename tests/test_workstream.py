import json

import pytest

from agentmgr import db, gitops
from agentmgr import workstream as ws_mod
from agentmgr.repos import RepoMatchError
from agentmgr.workstream import WorkstreamError

from .conftest import sh


def new(**kw):
    return ws_mod.new(launch=False, **kw)


def test_new_without_arguments_starts_intake(env):
    ws = new()
    assert ws.key == "WS-1"
    assert ws.path == env.workstreams / "WS-1"
    md = (ws.path / "CLAUDE.md").read_text()
    assert "## Intake" in md and "wm set WS-1" in md and "acme-ml-pipeline" in md
    settings = json.loads((ws.path / ".claude" / "settings.json").read_text())
    assert set(settings["hooks"]) >= {"SessionStart", "PreToolUse", "PostToolUse", "Notification", "Stop"}
    assert "EnterWorktree" in settings["permissions"]["deny"]
    assert f"Edit(//{str(env.code).strip('/')}/**)" in settings["permissions"]["deny"]
    assert new().key == "WS-2"


def test_memory_is_shared_across_workstreams(env, monkeypatch):
    memory = env.root / "home" / "memory"
    ws = new()
    assert f"## Memory\n\nYour memory folder, {memory}, is shared by every workstream" in (
        ws.path / "CLAUDE.md").read_text()
    cmd = ws_mod.claude_command(ws, resume=False)
    assert json.loads(cmd[cmd.index("--settings") + 1]) == {"autoMemoryDirectory": str(memory)}
    assert f"Edit(/{memory}/**)" in cmd[cmd.index("--allowedTools") + 1].split(",")
    # resuming regenerates CLAUDE.md, so older workstreams get template changes
    (ws.path / "CLAUDE.md").write_text("stale\n")
    monkeypatch.setattr(ws_mod, "session_alive", lambda ws: False)
    monkeypatch.setattr(ws_mod, "start_session", lambda ws, **kw: None)
    ws_mod.resume(ws.key)
    assert "## Memory" in (ws.path / "CLAUDE.md").read_text()


def test_add_repo_requires_name(env):
    ws = new()
    with pytest.raises(WorkstreamError, match="no name yet"):
        ws_mod.add_repo(ws.key, "web")


def test_intake_flow_creates_id_prefixed_worktrees(env):
    ws = new()
    ws_mod.set_(ws.key, name="Dark Mode Toggle", ticket="proj-313")
    res = ws_mod.add_repo(ws.key, "acme ML pipeline")
    ws_mod.add_repo(ws.key, "web")
    link = res.link
    assert res.created_branch
    assert link.branch == "proj-313-dark-mode-toggle"
    assert link.base == "origin/main"
    wt = ws.path / "acme-ml-pipeline"
    assert gitops.current_branch(wt) == link.branch
    md = (ws.path / "CLAUDE.md").read_text()
    assert "| acme-ml-pipeline/ | acme-ml-pipeline | proj-313-dark-mode-toggle |" in md
    assert (env.workstreams / "PROJ-313-dark-mode-toggle").resolve() == ws.path.resolve()
    # after TASK.md exists the intake section is replaced by the import
    (ws.path / "TASK.md").write_text("---\n---\n## Goal\n")
    ws_mod.set_(ws.key, name="dark mode toggle")
    md = (ws.path / "CLAUDE.md").read_text()
    assert "@TASK.md" in md and "## Intake" not in md


def test_new_with_ticket_name_and_repos(env):
    ws = new(ticket="PROJ-9", name="quick fix", repo_queries=["web"])
    assert ws.key == "PROJ-9"
    [link] = db.list_repos(db.connect(), "PROJ-9")
    assert link.branch == "proj-9-quick-fix"
    with pytest.raises(WorkstreamError, match="already used"):
        new(ticket="PROJ-9")


def test_bad_repo_rolls_back(env):
    with pytest.raises(RepoMatchError):
        new(ticket="PROJ-1", name="x", repo_queries=["nonexistent-thing"])
    assert not (env.workstreams / "PROJ-1").exists()
    assert db.get_workstream(db.connect(), "PROJ-1") is None


def test_existing_branch_rules(env):
    web = env.code / "acme-web"
    sh(web, "git", "branch", "feature-without-id")
    sh(web, "git", "branch", "proj-5-old-work")
    ws = new(ticket="PROJ-5", name="old work")
    with pytest.raises(WorkstreamError, match="does not contain PROJ-5"):
        ws_mod.add_repo(ws.key, "web", branch="feature-without-id")
    res = ws_mod.add_repo(ws.key, "web", branch="proj-5-old-work")
    assert not res.created_branch
    ws2 = new(ticket="PROJ-6", name="main stuff")
    with pytest.raises(WorkstreamError, match="already checked out"):
        ws_mod.add_repo(ws2.key, "web", branch="main", allow_foreign_branch=True)


def test_remove_repo_safety(env):
    ws = new(name="a b", repo_queries=["web"])
    wt = ws.path / "acme-web"
    (wt / "new.txt").write_text("x")
    with pytest.raises(WorkstreamError, match="uncommitted"):
        ws_mod.remove_repo(ws.key, "acme-web")
    (wt / "new.txt").unlink()
    ws_mod.remove_repo(ws.key, "acme-web")
    assert not wt.exists()
    assert not gitops.branch_exists(env.code / "acme-web", "ws-1-a-b")


def test_archive(env, monkeypatch):
    monkeypatch.setattr("agentmgr.gh.available", lambda: False)
    ws = new(ticket="PROJ-7", name="ship it", repo_queries=["web", "release"])
    wt = ws.path / "acme-web"
    (wt / "f.txt").write_text("x")
    sh(wt, "git", "add", ".")
    sh(wt, "git", "commit", "-q", "-m", "PROJ-7 work")
    with pytest.raises(WorkstreamError, match="unpushed"):
        ws_mod.archive(ws.key)
    # "merge" into main on origin
    sh(wt, "git", "push", "-q", "origin", "HEAD:main")
    log = ws_mod.archive(ws.key)
    assert any("deleted" in line for line in log)
    assert not wt.exists() and not (ws.path / "release-tools").exists()
    assert (ws.path / "CLAUDE.md").exists()  # folder and brief are kept
    assert db.get_workstream(db.connect(), ws.key).status == db.ARCHIVED
    assert not gitops.branch_exists(env.code / "acme-web", "proj-7-ship-it")
    assert not (env.workstreams / "PROJ-7-ship-it").exists()


def test_adopt_branch_with_uncommitted_changes(env):
    main = env.code / "acme-web"
    sh(main, "git", "switch", "-q", "-c", "proj-50-half-done")
    (main / "a.py").write_text("committed\n")
    sh(main, "git", "add", ".")
    sh(main, "git", "commit", "-q", "-m", "wip")
    (main / "a.py").write_text("committed\nuncommitted\n")
    (main / "new.txt").write_text("untracked\n")
    ws = new(ticket="PROJ-50", name="half done")
    plan = ws_mod.plan_adopt(ws.key, "web")
    assert plan.take_branch and plan.dirty and plan.free_to == "main"
    res = ws_mod.adopt(ws.key, plan)
    wt = ws.path / "acme-web"
    assert res.link.branch == "proj-50-half-done" and not res.link.created_branch
    assert gitops.current_branch(wt) == "proj-50-half-done"
    assert (wt / "a.py").read_text() == "committed\nuncommitted\n" and (wt / "new.txt").exists()
    # the main checkout is back on main, clean, and the temporary stash is gone
    assert gitops.current_branch(main) == "main" and not gitops.is_dirty(main)
    assert sh(main, "git", "stash", "list") == ""


def test_adopt_uncommitted_changes_on_main(env):
    main = env.code / "acme-web"
    (main / "README.md").write_text("edited on main\n")
    ws = new(name="quick fix")
    plan = ws_mod.plan_adopt(ws.key, "web")
    assert not plan.take_branch and plan.branch == "ws-1-quick-fix"
    ws_mod.adopt(ws.key, plan)
    assert (ws.path / "acme-web" / "README.md").read_text() == "edited on main\n"
    assert gitops.current_branch(main) == "main" and not gitops.is_dirty(main)


def test_adopt_refusals(env):
    main = env.code / "acme-web"
    ws = new(name="nothing here")
    with pytest.raises(WorkstreamError, match="nothing to adopt"):
        ws_mod.plan_adopt(ws.key, "web")
    sh(main, "git", "switch", "-q", "--detach")
    with pytest.raises(WorkstreamError, match="detached"):
        ws_mod.plan_adopt(ws.key, "web")


def test_adopt_rolls_back_on_failure(env, monkeypatch):
    main = env.code / "acme-web"
    sh(main, "git", "switch", "-q", "-c", "feature-x")
    (main / "README.md").write_text("dirty\n")
    ws = new(name="roll back")
    plan = ws_mod.plan_adopt(ws.key, "web")

    def boom(*a, **k):
        raise gitops.GitError("simulated")

    monkeypatch.setattr(gitops, "worktree_add", boom)
    with pytest.raises(gitops.GitError):
        ws_mod.adopt(ws.key, plan)
    assert gitops.current_branch(main) == "feature-x"
    assert (main / "README.md").read_text() == "dirty\n"
    assert sh(main, "git", "stash", "list") == ""
    assert db.list_repos(db.connect(), ws.key) == []


def test_fetcher_updates_behind_and_respects_max_age(env):
    from agentmgr import fetcher

    ws = new(name="behind check", repo_queries=["web"])
    wt = ws.path / "acme-web"
    # someone else pushes to main
    other = env.root / "other"
    sh(env.root, "git", "clone", "-q", str(env.origins / "acme-web.git"), str(other))
    (other / "x.txt").write_text("x")
    sh(other, "git", "add", ".")
    sh(other, "git", "commit", "-q", "-m", "upstream change")
    sh(other, "git", "push", "-q", "origin", "HEAD:main")

    assert gitops.status(wt, "origin/main").behind_base == 0  # stale until fetched
    assert fetcher.fetch_stale(0) == {str(env.code / "acme-web"): None}
    assert gitops.status(wt, "origin/main").behind_base == 1
    assert fetcher.fetch_stale(300) == {}  # fetched recently: skipped
    row = db.get_fetches(db.connect())[str(env.code / "acme-web")]
    assert row["ok_at"] and row["error"] is None


def test_fetch_failure_is_recorded(env):
    from agentmgr import fetcher

    new(name="broken remote", repo_queries=["web"])
    sh(env.code / "acme-web", "git", "remote", "set-url", "origin", str(env.root / "missing.git"))
    [(path, err)] = fetcher.fetch_stale(0).items()
    assert err
    row = db.get_fetches(db.connect())[path]
    assert row["error"] and row["ok_at"] is None


def test_git_timeout(env):
    with pytest.raises(gitops.GitError, match="timed out"):
        gitops.git(env.code / "acme-web", "-c", "alias.slow=!sleep 3", "slow", timeout=0.5)


def test_unarchive_restores_worktrees(env, monkeypatch):
    monkeypatch.setattr("agentmgr.gh.available", lambda: False)
    ws = new(ticket="PROJ-8", name="come back", repo_queries=["web", "release"])
    web, tools = ws.path / "acme-web", ws.path / "release-tools"
    (web / "f.txt").write_text("x")
    sh(web, "git", "add", ".")
    sh(web, "git", "commit", "-q", "-m", "PROJ-8 work")
    sh(web, "git", "push", "-q", "-u", "origin", "HEAD")  # pushed, not merged: the branch is kept
    ws_mod.archive(ws.key)  # release-tools's untouched branch is deleted
    assert not web.exists() and not gitops.branch_exists(env.code / "release-tools", "proj-8-come-back")

    with pytest.raises(WorkstreamError, match="no active workstream"):
        ws_mod.resolve(db.connect(), "PROJ-8")
    assert ws_mod.resolve(db.connect(), "come back", archived=True).key == ws.key

    log = ws_mod.unarchive(ws.key)
    assert any("acme-web: worktree restored on proj-8-come-back" in line for line in log)
    assert any("release-tools: proj-8-come-back was deleted; recreated from origin/main" in line for line in log)
    assert (web / "f.txt").read_text() == "x"
    assert gitops.current_branch(tools) == "proj-8-come-back"
    got = db.get_workstream(db.connect(), ws.key)
    assert got.status == db.STOPPED and got.archived_at is None
    assert (env.workstreams / "PROJ-8-come-back").is_symlink()
    with pytest.raises(WorkstreamError, match="not an archived"):
        ws_mod.unarchive(ws.key)


def test_unarchive_is_all_or_nothing(env, monkeypatch):
    monkeypatch.setattr("agentmgr.gh.available", lambda: False)
    ws = new(ticket="PROJ-9", name="blocked return", repo_queries=["web", "release"])
    for wt in (ws.path / "acme-web", ws.path / "release-tools"):
        (wt / "f.txt").write_text("x")
        sh(wt, "git", "add", ".")
        sh(wt, "git", "commit", "-q", "-m", "PROJ-9 work")
        sh(wt, "git", "push", "-q", "-u", "origin", "HEAD")
    ws_mod.archive(ws.key)
    # someone checked the branch out in the main checkout meanwhile
    sh(env.code / "release-tools", "git", "switch", "-q", "proj-9-blocked-return")
    with pytest.raises(WorkstreamError, match="checked out at"):
        ws_mod.unarchive(ws.key)
    assert not (ws.path / "acme-web").exists()
    assert db.get_workstream(db.connect(), ws.key).status == db.ARCHIVED

    new(ticket="PROJ-10", name="other")  # a ticket reused by an active workstream blocks the restore
    db.update_workstream(db.connect(), "PROJ-10", ticket="PROJ-9")
    sh(env.code / "release-tools", "git", "switch", "-q", "main")
    with pytest.raises(WorkstreamError, match="already used"):
        ws_mod.unarchive(ws.key)
