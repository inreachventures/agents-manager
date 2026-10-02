import pytest

from agentmgr import db, gitops, tmux
from agentmgr import workstream as ws_mod
from agentmgr.workstream import WorkstreamError

from .conftest import sh


def commit(wt, name, text, msg="work"):
    (wt / name).write_text(text)
    sh(wt, "git", "add", ".")
    sh(wt, "git", "commit", "-q", "-m", msg)
    return sh(wt, "git", "rev-parse", "HEAD")


def clone(env, name="other"):
    path = env.root / name
    sh(env.root, "git", "clone", "-q", str(env.origins / "acme-web.git"), str(path))
    return path


def remote_sha(wt, branch):
    return sh(wt, "git", "ls-remote", "origin", f"refs/heads/{branch}").split()[0]


@pytest.fixture
def ws(env):
    ws = ws_mod.new(launch=False, ticket="PROJ-4", name="rebase me", repo_queries=["web"])
    wt = ws.path / "acme-web"
    commit(wt, "a.txt", "a")
    sh(wt, "git", "push", "-q", "-u", "origin", "HEAD")
    return ws


def test_rebase_then_force_push(env, ws):
    wt = ws.path / "acme-web"
    other = clone(env)
    commit(other, "b.txt", "b", "main moved")
    sh(other, "git", "push", "-q", "origin", "HEAD:main")

    _, [step] = ws_mod.rebase_plan(ws.key)
    assert (step.behind, step.skip, step.pushed_at) == (1, None, sh(wt, "git", "rev-parse", "HEAD"))
    assert ws_mod.rebase(ws.key) == ["acme-web: rebased onto origin/main, force-pushed"]
    assert gitops.status(wt, "origin/main").behind_base == 0
    assert (wt / "b.txt").exists() and (wt / "a.txt").exists()
    assert remote_sha(wt, "proj-4-rebase-me") == sh(wt, "git", "rev-parse", "HEAD")
    assert ws_mod.rebase_plan(ws.key)[1][0].skip == "up to date with origin/main"


def test_push_lease_protects_commits_pushed_meanwhile(env, ws):
    wt = ws.path / "acme-web"
    other = clone(env)
    commit(other, "b.txt", "b", "main moved")
    sh(other, "git", "push", "-q", "origin", "HEAD:main")
    ws_mod.rebase_plan(ws.key)  # shown for confirmation; fetches
    # someone pushes to the PR branch before you confirm
    sh(other, "git", "fetch", "-q", "origin")
    sh(other, "git", "switch", "-q", "proj-4-rebase-me")
    theirs = commit(other, "c.txt", "c", "their fix")
    sh(other, "git", "push", "-q", "origin", "HEAD")

    [line] = ws_mod.rebase(ws.key)
    assert line == ("acme-web: rebased onto origin/main, but not pushed: someone else pushed to "
                    "origin/proj-4-rebase-me meanwhile, so it was left as it is")
    assert remote_sha(wt, "proj-4-rebase-me") == theirs  # not overwritten


def test_conflicts_abort_and_go_to_claude(env, ws, monkeypatch):
    wt = ws.path / "acme-web"
    head = commit(wt, "README.md", "ours\n")
    other = clone(env)
    commit(other, "README.md", "theirs\n", "main moved")
    sh(other, "git", "push", "-q", "origin", "HEAD:main")
    sent = []
    monkeypatch.setattr(ws_mod, "session_alive", lambda ws: True)
    monkeypatch.setattr("agentmgr.tmux.send_text", lambda name, text: sent.append((name, text)))

    ws_mod.rebase_plan(ws.key)
    assert ws_mod.rebase(ws.key) == [
        "acme-web: conflicts in README.md; rebase aborted, nothing changed",
        "asked Claude to resolve the conflicts",
    ]
    assert sh(wt, "git", "rev-parse", "HEAD") == head and gitops.operation_in_progress(wt) is None
    [(name, text)] = sent
    assert name == ws.key and "acme-web/ onto origin/main (README.md)" in text


def test_conflicts_resume_a_stopped_session(env, ws, monkeypatch):
    wt = ws.path / "acme-web"
    commit(wt, "README.md", "ours\n")
    other = clone(env)
    commit(other, "README.md", "theirs\n", "main moved")
    sh(other, "git", "push", "-q", "origin", "HEAD:main")
    started = []
    monkeypatch.setattr(ws_mod, "session_alive", lambda ws: False)
    monkeypatch.setattr(ws_mod, "start_session", lambda ws, **kw: started.append(kw))

    ws_mod.rebase_plan(ws.key)
    assert ws_mod.rebase(ws.key)[-1] == "resumed the session and asked Claude to resolve the conflicts"
    [kw] = started
    assert "README.md" in kw["initial_prompt"]


def test_what_is_skipped(env, ws, monkeypatch):
    wt = ws.path / "acme-web"
    other = clone(env)
    commit(other, "b.txt", "b", "main moved")
    sh(other, "git", "push", "-q", "origin", "HEAD:main")

    (wt / "a.txt").write_text("edited")
    assert ws_mod.rebase_plan(ws.key)[1][0].skip == "1 uncommitted change(s)"
    sh(wt, "git", "checkout", "-q", "--", "a.txt")

    sh(other, "git", "switch", "-q", "proj-4-rebase-me")
    commit(other, "c.txt", "c", "their fix")
    sh(other, "git", "push", "-q", "origin", "HEAD")
    assert ws_mod.rebase_plan(ws.key)[1][0].skip == "1 commit(s) on origin/proj-4-rebase-me that aren't local"

    db.upsert_pr(db.connect(), db.PR(ws.key, "acme-web", 5, "merged", False, None, "pass", None, 0))
    assert ws_mod.rebase_plan(ws.key)[1][0].skip == "PR already merged"

    db.set_status(db.connect(), ws.key, db.WORKING)
    monkeypatch.setattr(ws_mod, "session_alive", lambda ws: True)
    with pytest.raises(WorkstreamError, match="middle of a task"):
        ws_mod.rebase_plan(ws.key)


@pytest.mark.skipif(not tmux.available(), reason="needs tmux")
def test_send_text_types_into_the_session(env):
    try:
        tmux.new_session("T-1", env.root, ["cat"])
        tmux.send_text("T-1", "hello [wm] #1 'quoted'")
        out = tmux.run("capture-pane", "-p", "-t", "=T-1:").stdout
        assert out.count("hello [wm] #1 'quoted'") == 2  # echoed while typing, then printed by cat after Enter
    finally:
        tmux.run("kill-server", check=False)
