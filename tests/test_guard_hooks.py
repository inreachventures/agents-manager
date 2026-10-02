import io
import json
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
    monkeypatch.setattr("agentmgr.tmux.set_session_option", lambda *a: shown.append(a))
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
