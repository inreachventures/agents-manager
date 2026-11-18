import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentmgr import db, guard, hooks
from agentmgr import workstream as ws_mod

from .conftest import sh


@pytest.fixture
def ctx(tmp_path):
    folder = tmp_path / "workstreams" / "WS-1"
    return guard.Context(ws_key="WS-1", folder=folder, cwd=folder, code_roots=[tmp_path / "code"],
                         branches={"acme-web": "ws-1-x"})


def test_file_tools(ctx, tmp_path):
    ok = guard.check_file_tool("Edit", {"file_path": str(ctx.folder / "acme-web/a.py")}, ctx)
    assert ok is None
    assert guard.check_file_tool("Write", {"file_path": "TASK.md"}, ctx) is None
    assert "outside" in guard.check_file_tool("Edit", {"file_path": str(tmp_path / "code/acme-web/a.py")}, ctx)
    assert "outside" in guard.check_file_tool("Write", {"file_path": "../../x"}, ctx)
    assert "generated" in guard.check_file_tool("Edit", {"file_path": "CLAUDE.md"}, ctx)


def test_shared_memory_and_scratchpad_are_writable(ctx, tmp_path):
    memory, scratch = tmp_path / "home" / "memory", tmp_path / "scratch"
    ctx.writable = [memory, scratch]
    assert guard.check_file_tool("Write", {"file_path": str(memory / "MEMORY.md")}, ctx) is None
    assert guard.check_file_tool("Write", {"file_path": str(scratch / "s1" / "scratchpad" / "x.py")}, ctx) is None
    assert "outside" in guard.check_file_tool("Write", {"file_path": str(memory / ".." / "config.toml")}, ctx)
    memory.mkdir(parents=True)
    (memory / "code").symlink_to(tmp_path / "code")
    assert "outside" in guard.check_file_tool("Edit", {"file_path": str(memory / "code" / "a.py")}, ctx)


@pytest.mark.parametrize("cmd", [
    "git worktree add ../x",
    "cd acme-web && git switch main",
    "cd acme-web && git checkout main",
    "git -C acme-web checkout -b other",
    "cd ~/code/foo",
    "FOO=1 git -C {code}/acme-web commit -m x",
    "cd {code}/acme-web; ls",
    "git branch -m newname",
])
def test_bash_blocked(ctx, cmd, tmp_path):
    ctx.code_roots.append(Path.home() / "code")
    assert guard.check_bash(cmd.format(code=tmp_path / "code"), ctx) is not None


@pytest.mark.parametrize("cmd", [
    "cd acme-web && git status && git commit -am 'PROJ-1 x'",
    "git -C acme-web checkout -- src/a.py",
    "git worktree list",
    "cat {code}/acme-web/README.md",
    "git -C {code}/acme-web log --oneline -5",
    "cd acme-web && git checkout .",
    "echo 'git worktree add' > notes.txt",
    "wm add-repo WS-1 web",
])
def test_bash_allowed(ctx, cmd, tmp_path):
    assert guard.check_bash(cmd.format(code=tmp_path / "code"), ctx) is None


def test_agent_isolation(ctx):
    assert guard.check_agent({"isolation": "worktree"}, ctx)
    assert guard.check_agent({"prompt": "x"}, ctx) is None


def run_hook(monkeypatch, capsys, event, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks.main(event)
    return capsys.readouterr().out


def test_hook_status_and_guard(env, monkeypatch, capsys):
    monkeypatch.setattr("agentmgr.notify.notify", lambda *a: None)
    ws = ws_mod.new(launch=False, name="x y", repo_queries=["web"])
    base = {"session_id": ws.session_id, "cwd": ws.folder}
    conn = db.connect()

    out = run_hook(monkeypatch, capsys, "SessionStart", base)
    assert "acme-web/: on ws-1-x-y" in out and "Intake is not complete" in out
    assert db.get_workstream(conn, ws.key).status == db.YOUR_TURN

    run_hook(monkeypatch, capsys, "UserPromptSubmit", base)
    assert db.get_workstream(conn, ws.key).status == db.WORKING

    out = run_hook(monkeypatch, capsys, "PreToolUse", {**base, "tool_name": "Edit",
                   "tool_input": {"file_path": str(env.code / "acme-web" / "README.md")}})
    decision = json.loads(out)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    for allowed in (env.root / "home" / "memory" / "MEMORY.md",
                    hooks.scratch_dir(ws) / ws.session_id / "scratchpad" / "check.py"):
        assert run_hook(monkeypatch, capsys, "PreToolUse", {**base, "tool_name": "Write",
                        "tool_input": {"file_path": str(allowed)}}) == ""

    run_hook(monkeypatch, capsys, "Notification", {**base, "notification_type": "permission_prompt",
                                                   "message": "Claude needs permission to use Bash"})
    got = db.get_workstream(conn, ws.key)
    assert got.status == db.NEEDS_YOU and "permission" in got.status_reason

    run_hook(monkeypatch, capsys, "PostToolUse", {**base, "tool_name": "Read"})
    assert db.get_workstream(conn, ws.key).status == db.WORKING

    run_hook(monkeypatch, capsys, "Stop", base)
    assert db.get_workstream(conn, ws.key).status == db.YOUR_TURN

    # unmanaged sessions are ignored
    assert run_hook(monkeypatch, capsys, "PreToolUse", {"session_id": "other", "cwd": "/tmp",
                    "tool_name": "Bash", "tool_input": {"command": "git worktree add x"}}) == ""


def test_drift_detection(env, monkeypatch, capsys):
    ws = ws_mod.new(launch=False, name="x y", repo_queries=["web"])
    wt = Path(ws.folder) / "acme-web"
    sh(wt, "git", "checkout", "-q", "-b", "sneaky")
    out = run_hook(monkeypatch, capsys, "PostToolUse", {"session_id": ws.session_id, "cwd": ws.folder,
                                                         "tool_name": "Bash"})
    assert json.loads(out)["decision"] == "block"
    assert "sneaky" in db.get_workstream(db.connect(), ws.key).drift
    sh(wt, "git", "checkout", "-q", "ws-1-x-y")
    assert run_hook(monkeypatch, capsys, "PostToolUse", {"session_id": ws.session_id, "cwd": ws.folder,
                                                          "tool_name": "Bash"}) == ""
    assert db.get_workstream(db.connect(), ws.key).drift is None


def test_unattached_repo_redirects_to_add_repo(ctx, tmp_path):
    other = tmp_path / "code" / "acme-mobile" / "src" / "app.ts"
    msg = guard.check_file_tool("Edit", {"file_path": str(other)}, ctx)
    assert "wm add-repo WS-1 acme-mobile" in msg
    msg = guard.check_file_tool("Edit", {"file_path": str(tmp_path / "code" / "acme-web" / "a.py")}, ctx)
    assert str(ctx.folder / "acme-web" / "a.py") in msg
    assert "wm add-repo WS-1 acme-mobile" in guard.check_bash(f"cd {tmp_path}/code/acme-mobile", ctx)
    assert "wm add-repo" in guard.check_bash("git clone git@github.com:x/acme-mobile.git", ctx)


def test_unattached_checkout_in_folder_is_drift(env, monkeypatch, capsys):
    ws = ws_mod.new(launch=False, name="x y", repo_queries=["web"])
    sh(ws.folder, "git", "clone", "-q", str(env.origins / "acme-ml-pipeline.git"))
    out = run_hook(monkeypatch, capsys, "PostToolUse", {"session_id": ws.session_id, "cwd": ws.folder,
                                                         "tool_name": "Bash"})
    assert "not attached" in json.loads(out)["reason"]


def test_prompt_bar(env, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("agentmgr.notify.notify", lambda *a: None)
    shown = []
    monkeypatch.setattr("agentmgr.tmux.available", lambda: True)
    monkeypatch.setattr("agentmgr.tmux.set_window_option", lambda *a: shown.append(a))
    ws = ws_mod.new(launch=False, name="x y", repo_queries=["web"])
    base = {"session_id": ws.session_id, "cwd": ws.folder}

    run_hook(monkeypatch, capsys, "UserPromptSubmit", {**base, "prompt": "fix  issue #34\nthen\trebase"})
    assert shown[-1] == (ws.key, hooks.PROMPT_OPTION, "fix issue ##34 then rebase")

    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(json.dumps(e) for e in [
        {"type": "user", "message": {"content": "first ask"}},
        {"type": "user", "message": {"content": "second ask"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "out"}]}},
        {"type": "user", "isMeta": True, "message": {"content": "caveat"}},
        {"type": "user", "message": {"content": "<command-name>/clear</command-name>"}},
    ]) + "\nnot json\n")
    assert hooks.last_prompt(transcript) == "second ask"
    assert hooks.last_prompt(tmp_path / "missing.jsonl") == ""

    run_hook(monkeypatch, capsys, "SessionStart", {**base, "transcript_path": str(transcript)})
    assert shown[-1] == (ws.key, hooks.PROMPT_OPTION, "second ask")


def test_compaction_keeps_status_and_refreshes_claude_md(env, monkeypatch, capsys):
    ws = ws_mod.new(launch=False, name="x y", repo_queries=["web"])
    base = {"session_id": ws.session_id, "cwd": ws.folder}
    conn = db.connect()
    run_hook(monkeypatch, capsys, "UserPromptSubmit", base)
    (ws.path / "acme-web" / "CLAUDE.md").write_text("# acme-web rules\n")  # appeared after the repo was attached

    # auto-compaction happens while Claude works: the session isn't waiting for the user
    out = run_hook(monkeypatch, capsys, "SessionStart", {**base, "source": "compact"})
    assert "acme-web/: on ws-1-x-y" in out
    assert db.get_workstream(conn, ws.key).status == db.WORKING
    assert "@acme-web/CLAUDE.md" in (ws.path / "CLAUDE.md").read_text()

    run_hook(monkeypatch, capsys, "SessionStart", {**base, "source": "resume"})
    assert db.get_workstream(conn, ws.key).status == db.YOUR_TURN

    # /clear re-reads CLAUDE.md too
    (ws.path / "acme-web" / "CLAUDE.md").unlink()
    run_hook(monkeypatch, capsys, "SessionStart", {**base, "source": "clear"})
    assert "@acme-web/CLAUDE.md" not in (ws.path / "CLAUDE.md").read_text()

    # nothing changed: nothing is rewritten (Claude Code may be reading the file right then)
    before = (ws.path / "CLAUDE.md").stat().st_mtime_ns, (ws.path / ".claude" / "settings.json").stat().st_mtime_ns
    run_hook(monkeypatch, capsys, "SessionStart", {**base, "source": "compact"})
    assert before == ((ws.path / "CLAUDE.md").stat().st_mtime_ns,
                      (ws.path / ".claude" / "settings.json").stat().st_mtime_ns)

    # a failure to regenerate is logged and still leaves Claude the live state
    def broken(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(ws_mod, "regenerate", broken)
    out = run_hook(monkeypatch, capsys, "SessionStart", {**base, "source": "compact"})
    assert "acme-web/: on ws-1-x-y" in out
    assert "regenerate: OSError: disk full" in (env.root / "home" / "hooks.log").read_text()


def test_a_turn_that_ends_while_you_look_is_seen_and_not_notified(env, monkeypatch, capsys):
    sent = []
    monkeypatch.setattr("agentmgr.notify.notify", lambda *a: sent.append(a))
    ws = ws_mod.new(launch=False, name="x y")
    base = {"session_id": ws.session_id, "cwd": ws.folder}
    conn = db.connect()
    looking = set()
    monkeypatch.setattr("agentmgr.tmux.watched_sessions", lambda shown: looking)

    run_hook(monkeypatch, capsys, "UserPromptSubmit", base)
    looking.add(ws.key)
    run_hook(monkeypatch, capsys, "Stop", base)
    assert sent == [] and db.seen_times(conn)[ws.key] >= db.get_workstream(conn, ws.key).status_at

    run_hook(monkeypatch, capsys, "UserPromptSubmit", base)
    looking.clear()
    run_hook(monkeypatch, capsys, "Stop", base)
    assert sent == [(ws.label, "finished, your turn")]
    assert db.seen_times(conn)[ws.key] < db.get_workstream(conn, ws.key).status_at


def _commit(wt, name):
    (wt / name).write_text(name)
    sh(wt, "git", "add", name)
    sh(wt, "git", "commit", "-q", "-m", name)


def _draw_env(env, monkeypatch):
    from agentmgr import taskmap

    ws = ws_mod.new(launch=False, name="x y", repo_queries=["web"])
    calls = []
    monkeypatch.setattr(taskmap, "start_drawing", lambda conn, w: calls.append(w.key) or "started")
    base = {"session_id": ws.session_id, "cwd": ws.folder}
    return ws, base, calls, Path(ws.folder) / "acme-web"


def _commit_payload(base, **extra):
    return {**base, "tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, **extra}


def test_drawing_process_hooks_are_ignored(env, monkeypatch, capsys):
    monkeypatch.setenv("AGENTMGR_MAP_DRAWING", "1")
    ws = ws_mod.new(launch=False, name="x y", repo_queries=["web"])
    base = {"session_id": ws.session_id, "cwd": ws.folder}
    conn = db.connect()
    db.set_status(conn, ws.key, db.NEEDS_YOU, "waiting")
    for event, payload in (("PostToolUse", {**base, "tool_name": "Bash", "tool_input": {"command": "ls"}}),
                           ("Stop", base)):
        assert run_hook(monkeypatch, capsys, event, payload) == ""
        assert db.get_workstream(conn, ws.key).status == db.NEEDS_YOU


def test_commit_starts_drawing(env, monkeypatch, capsys):
    ws, base, calls, wt = _draw_env(env, monkeypatch)
    _commit(wt, "a.txt")
    assert run_hook(monkeypatch, capsys, "PostToolUse", _commit_payload(base)) == ""
    assert calls == [ws.key]


@pytest.mark.parametrize("command", ["git merge origin/main", "git rebase origin/main", "git pull --rebase",
                                     "git cherry-pick abc123", "git revert HEAD", "git reset --hard HEAD~1"])
def test_other_head_moving_commands_start_drawing(env, monkeypatch, capsys, command):
    ws, base, calls, wt = _draw_env(env, monkeypatch)
    _commit(wt, "a.txt")  # the head moved, as the command would have moved it
    payload = {**base, "tool_name": "Bash", "tool_input": {"command": command}}
    assert run_hook(monkeypatch, capsys, "PostToolUse", payload) == ""
    assert calls == [ws.key]


def test_commit_from_subagent_payload_starts_drawing(env, monkeypatch, capsys):
    ws, base, calls, wt = _draw_env(env, monkeypatch)
    _commit(wt, "a.txt")
    payload = _commit_payload(base, agent_id="sub-1", agent_type="general-purpose")
    assert run_hook(monkeypatch, capsys, "PostToolUse", payload) == ""
    assert calls == [ws.key]


def test_empty_commit_does_not_draw(env, monkeypatch, capsys):
    from agentmgr import taskmap

    ws, base, calls, wt = _draw_env(env, monkeypatch)
    _commit(wt, "a.txt")
    snap = taskmap.snapshot(db.connect(), ws)
    taskmap._write_json(taskmap.map_dir(ws) / "map.json", {
        "doc": {}, "drawn_at": 0, "token": "t",
        "snapshot": {k: vars(v) for k, v in snap.items()}})
    assert run_hook(monkeypatch, capsys, "PostToolUse", _commit_payload(base)) == ""
    log = {**base, "tool_name": "Bash", "tool_input": {"command": "git log --oneline"}}
    assert run_hook(monkeypatch, capsys, "PostToolUse", log) == ""
    assert calls == []
    _commit(wt, "b.txt")
    run_hook(monkeypatch, capsys, "PostToolUse", _commit_payload(base))
    assert calls == [ws.key]


def test_hooks_do_not_import_the_task_map_up_front():
    """Every tool call runs a hook: the task map (and the workstream and diff modules it pulls in) loads only when a
    commit may need a drawing."""
    heavy = ["agentmgr.taskmap", "agentmgr.workstream", "agentmgr.diffs"]
    code = f"import sys, agentmgr.hooks; print(sorted(m for m in sys.modules if m in {heavy!r}))"
    assert subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          check=True).stdout.strip() == "[]"


def test_a_failed_drawing_is_not_retried_until_a_repo_moves(env, monkeypatch, capsys):
    """With no map yet, a command that only looks like a commit (nothing to commit, `git log --grep commit`) must not
    start a drawing of what the last, failed drawing already tried: each would spawn another `claude -p`."""
    import shutil

    from agentmgr import taskmap

    ws = ws_mod.new(launch=False, name="x y", repo_queries=["web"])
    base = {"session_id": ws.session_id, "cwd": ws.folder}
    wt = Path(ws.folder) / "acme-web"
    spawned = []
    real_which = shutil.which
    monkeypatch.setattr(taskmap.shutil, "which", lambda cmd, *a, **k: (
        f"/fake/bin/{cmd}" if cmd in ("node", "claude") else real_which(cmd, *a, **k)))
    monkeypatch.setattr(taskmap, "_spawn", lambda argv, cwd, env, log, limit: spawned.append(argv) or (4201, 4101))
    monkeypatch.setattr(taskmap, "_alive", lambda pid, pgid: False)  # every drawing dies at once without saving

    _commit(wt, "a.txt")
    run_hook(monkeypatch, capsys, "PostToolUse", _commit_payload(base))
    assert len(spawned) == 1
    taskmap.reap(db.connect(), ws)  # the server's loop notices it ended
    assert taskmap.last_failure(ws) is not None and taskmap.load(ws) is None
    for command in ("git commit -m x", "git commit --help", "git log --grep commit"):
        run_hook(monkeypatch, capsys, "PostToolUse", {**base, "tool_name": "Bash", "tool_input": {"command": command}})
    assert len(spawned) == 1
    _commit(wt, "b.txt")
    run_hook(monkeypatch, capsys, "PostToolUse", _commit_payload(base))
    assert len(spawned) == 2
