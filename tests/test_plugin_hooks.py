import io
import json
import shutil
from pathlib import Path

import pytest

from agentmgr import config, db, hooks, plugins
from agentmgr import workstream as ws_mod
from agentmgr.plugin import HookResult

from .conftest import sh
from .test_guard_hooks import run_hook

FIXTURE = Path(__file__).parent / "fixtures" / "plugins" / "demo"


@pytest.fixture(autouse=True)
def fresh_plugins():
    plugins.reset()
    yield
    plugins.reset()
    plugins.set_logger(plugins.stderr_logger)


@pytest.fixture
def pdir(env, monkeypatch):
    d = env.root / "plugins"
    shutil.copytree(FIXTURE, d / "demo")
    monkeypatch.setenv("AGENTMGR_PLUGINS_DIR", str(d))
    monkeypatch.delenv("AGENTMGR_AUX", raising=False)
    config.get_config.cache_clear()
    return d


@pytest.fixture
def sent(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr("agentmgr.notify.notify", lambda *a: calls.append(a))
    return calls


@pytest.fixture
def ws(pdir, sent):
    return ws_mod.new(launch=False, name="x y", repo_queries=["web"])


def base(ws) -> dict:
    return {"session_id": ws.session_id, "cwd": ws.folder}


def data(ws, plugin="demo") -> Path:
    d = ws.path / ".wm" / plugin
    d.mkdir(parents=True, exist_ok=True)
    return d


def status(ws) -> db.Workstream:
    return db.get_workstream(db.connect(), ws.key)


def hook_log(env) -> list[str]:
    f = env.root / "home" / "hooks.log"
    return f.read_text().splitlines() if f.exists() else []


def spy(event: str, name: str = "demo") -> list:
    """Wrap a loaded plugin's handler to record the contexts it is called with."""
    seen = []
    (lp,) = [lp for lp in plugins.loaded(event=event) if lp.manifest.name == name]
    original = lp.api.registered.hooks[event]

    def wrapper(ctx):
        seen.append(ctx)
        return original(ctx)

    lp.api.registered.hooks[event] = wrapper
    return seen


def test_post_tool_use_context_reaches_claude(ws, monkeypatch, capsys):
    (data(ws) / "inbox.txt").write_text("the build finished")
    out = run_hook(monkeypatch, capsys, "PostToolUse", {**base(ws), "tool_name": "Read"})
    assert json.loads(out) == {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                      "additionalContext": "the build finished"}}
    assert not (data(ws) / "inbox.txt").exists()


def test_core_block_wins_and_plugins_not_called(ws, monkeypatch, capsys):
    (data(ws) / "inbox.txt").write_text("the build finished")
    sh(Path(ws.folder) / "acme-web", "git", "checkout", "-q", "-b", "sneaky")
    out = json.loads(run_hook(monkeypatch, capsys, "PostToolUse", {**base(ws), "tool_name": "Bash"}))
    assert out["decision"] == "block" and "drifted" in out["reason"] and "hookSpecificOutput" not in out
    assert (data(ws) / "inbox.txt").read_text() == "the build finished"


def test_stop_waiting_on_is_quiet(ws, monkeypatch, capsys, sent):
    run_hook(monkeypatch, capsys, "UserPromptSubmit", base(ws))
    (data(ws) / "job.txt").write_text("waiting on the eval run")
    assert run_hook(monkeypatch, capsys, "Stop", base(ws)) == ""
    got = status(ws)
    assert (got.status, got.status_reason) == (db.YOUR_TURN, "waiting on the eval run")
    assert sent == []


def test_stop_without_plugin_result_notifies(ws, monkeypatch, capsys, sent):
    run_hook(monkeypatch, capsys, "UserPromptSubmit", base(ws))
    run_hook(monkeypatch, capsys, "Stop", base(ws))
    assert status(ws).status == db.YOUR_TURN
    assert sent == [(ws.label, "finished, your turn")]


def test_stop_block_keeps_working(ws, monkeypatch, capsys, sent):
    run_hook(monkeypatch, capsys, "UserPromptSubmit", base(ws))
    (data(ws) / "block.txt").write_text("finish the migration first")
    out = run_hook(monkeypatch, capsys, "Stop", base(ws))
    assert json.loads(out) == {"decision": "block", "reason": "finish the migration first"}
    assert status(ws).status == db.WORKING
    assert sent == []


def test_raising_handler_keeps_core(ws, env, monkeypatch, capsys):
    monkeypatch.setenv("DEMO_RAISE", "1")
    run_hook(monkeypatch, capsys, "Notification", {**base(ws), "notification_type": "permission_prompt",
                                                    "message": "Claude needs permission to use Bash"})
    assert status(ws).status == db.NEEDS_YOU
    out = run_hook(monkeypatch, capsys, "PostToolUse", {**base(ws), "tool_name": "Read"})
    assert out == "" or json.loads(out) is not None
    assert status(ws).status == db.WORKING
    assert [ln for ln in hook_log(env) if ln.startswith("plugin demo:")] == [
        "plugin demo: hook PostToolUse: RuntimeError: boom"]


def test_load_failure_is_logged_on_each_hook_call(ws, env, pdir, monkeypatch, capsys):
    (pdir / "demo" / "plugin.py").write_text("raise ImportError('no such thing')\n")
    plugins.reset()  # creating ws loaded the plugin; a hook process starts fresh
    run_hook(monkeypatch, capsys, "PostToolUse", {**base(ws), "tool_name": "Read"})
    run_hook(monkeypatch, capsys, "PostToolUse", {**base(ws), "tool_name": "Read"})
    run_hook(monkeypatch, capsys, "UserPromptSubmit", base(ws))  # demo doesn't declare it: nothing to log
    assert [ln for ln in hook_log(env) if ln.startswith("plugin demo:")] == [
        "plugin demo: skipped: ImportError: no such thing"] * 2
    assert status(ws).status == db.WORKING


def install_demo2(pdir):
    shutil.copytree(FIXTURE, pdir / "demo2")
    toml = pdir / "demo2" / "plugin.toml"
    toml.write_text(toml.read_text().replace('name = "demo"', 'name = "demo2"')
                    .replace('command = "demo"', 'command = "demo2"'))


def test_two_plugins_first_block_wins(ws, pdir, monkeypatch, capsys):
    install_demo2(pdir)
    (data(ws) / "block.txt").write_text("demo says keep going")
    (data(ws, "demo2") / "block.txt").write_text("demo2 says keep going")
    seen = spy("Stop", "demo2")
    out = run_hook(monkeypatch, capsys, "Stop", base(ws))
    assert json.loads(out) == {"decision": "block", "reason": "demo says keep going"}
    assert len(seen) == 1  # later plugins are still called

    (data(ws) / "inbox.txt").write_text("from demo")
    (data(ws, "demo2") / "inbox.txt").write_text("from demo2")
    out = run_hook(monkeypatch, capsys, "PostToolUse", {**base(ws), "tool_name": "Read"})
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == "from demo\n\nfrom demo2"


def test_merge():
    merged = hooks.merge([HookResult(context="a"), HookResult(block="first", waiting_on="w1"),
                          HookResult(context="b", deny="d", block="second", waiting_on="w2")])
    assert merged == HookResult(context="a\n\nb", deny="d", block="first", waiting_on="w1")
    assert hooks.merge([]) == HookResult()


def test_aux_session(ws, pdir, env, monkeypatch, capsys, sent):
    run_hook(monkeypatch, capsys, "UserPromptSubmit", base(ws))
    before = status(ws)
    monkeypatch.setenv("AGENTMGR_AUX", "demo")
    seen = spy("Stop")
    aux = {"session_id": "aux-session", "cwd": ws.folder}

    (data(ws) / "job.txt").write_text("waiting on the eval run")
    run_hook(monkeypatch, capsys, "Stop", aux)
    run_hook(monkeypatch, capsys, "UserPromptSubmit", aux)
    run_hook(monkeypatch, capsys, "Notification", {**aux, "notification_type": "permission_prompt"})
    run_hook(monkeypatch, capsys, "SessionEnd", aux)
    assert run_hook(monkeypatch, capsys, "SessionStart", aux) == ""
    after = status(ws)
    assert (after.status, after.status_reason, after.status_at) == (before.status, before.status_reason,
                                                                    before.status_at)
    assert sent == []
    assert [ctx.aux for ctx in seen] == [True]

    out = run_hook(monkeypatch, capsys, "PreToolUse", {**aux, "tool_name": "Edit",
                   "tool_input": {"file_path": str(env.code / "acme-web" / "README.md")}})
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert run_hook(monkeypatch, capsys, "PreToolUse", {**aux, "tool_name": "Edit",
                    "tool_input": {"file_path": str(ws.path / ".wm" / "demo" / "x")}}) == ""
    install_demo2(pdir)
    out = run_hook(monkeypatch, capsys, "PreToolUse", {**aux, "tool_name": "Edit",
                   "tool_input": {"file_path": str(ws.path / ".wm" / "demo2" / "x")}})
    assert "belongs to the demo2 plugin" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]

    sh(Path(ws.folder) / "acme-web", "git", "checkout", "-q", "-b", "sneaky")
    out = run_hook(monkeypatch, capsys, "PostToolUse", {**aux, "tool_name": "Bash"})
    assert json.loads(out)["decision"] == "block"
    sh(Path(ws.folder) / "acme-web", "git", "checkout", "-q", "ws-1-x-y")

    monkeypatch.setenv("AGENTMGR_AUX", "other")
    run_hook(monkeypatch, capsys, "Stop", aux)
    assert len(seen) == 1
    assert (data(ws) / "job.txt").exists()
    assert status(ws).status == before.status


def test_protected_dir_blocks_main_session(ws, monkeypatch, capsys):
    out = run_hook(monkeypatch, capsys, "PreToolUse", {**base(ws), "tool_name": "Edit",
                   "tool_input": {"file_path": str(ws.path / ".wm" / "demo" / "notes.json")}})
    assert json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] == (
        "Blocked: .wm/demo/notes.json belongs to the demo plugin; change it with its wm commands.")
    assert run_hook(monkeypatch, capsys, "PreToolUse", {**base(ws), "tool_name": "Edit",
                    "tool_input": {"file_path": str(ws.path / ".wm" / "other" / "x")}}) == ""


def test_session_start_context_is_appended(ws, pdir, monkeypatch, capsys):
    toml = pdir / "demo" / "plugin.toml"
    toml.write_text(toml.read_text().replace('hooks = ["PostToolUse", "Stop"]',
                                             'hooks = ["SessionStart", "PostToolUse", "Stop"]'))
    (lp,) = plugins.loaded(event="SessionStart")  # register() doesn't hook SessionStart: add it here
    lp.api.registered.hooks["SessionStart"] = lambda ctx: HookResult(context="demo: 2 notes")
    out = run_hook(monkeypatch, capsys, "SessionStart", base(ws))
    assert "acme-web/: on ws-1-x-y" in out
    assert out.rstrip().endswith("\n\ndemo: 2 notes")


def test_quiet_wait_is_seen(ws, monkeypatch, capsys):
    monkeypatch.setattr("agentmgr.tmux.watched_sessions", lambda shown: set())  # nobody is looking
    run_hook(monkeypatch, capsys, "UserPromptSubmit", base(ws))
    (data(ws) / "job.txt").write_text("waiting on the eval run")
    run_hook(monkeypatch, capsys, "Stop", base(ws))
    conn = db.connect()
    got = db.get_workstream(conn, ws.key)
    assert got.status == db.YOUR_TURN and not db.unseen(conn, got)

    (data(ws) / "job.txt").unlink()  # a plain Stop nobody watches still counts as unseen
    run_hook(monkeypatch, capsys, "UserPromptSubmit", base(ws))
    run_hook(monkeypatch, capsys, "Stop", base(ws))
    assert db.unseen(conn, db.get_workstream(conn, ws.key))


def test_malformed_plugins_config_keeps_core(ws, env, monkeypatch, capsys, sent):
    cfg = config.get_config()
    cfg.config_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.config_file.write_text('plugins = ["x"]\n')
    config.get_config.cache_clear()
    assert [m.name for m in plugins.discover()[0]] == ["demo"]

    run_hook(monkeypatch, capsys, "UserPromptSubmit", base(ws))
    out = run_hook(monkeypatch, capsys, "PreToolUse", {**base(ws), "tool_name": "Edit",
                   "tool_input": {"file_path": str(env.code / "acme-web" / "README.md")}})
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    run_hook(monkeypatch, capsys, "Stop", base(ws))
    assert status(ws).status == db.YOUR_TURN


def test_plugin_failure_outside_handlers_keeps_core(ws, env, monkeypatch, capsys):
    def broken(*a, **kw):
        raise OSError("plugins dir unreadable")
    monkeypatch.setattr(plugins, "discover", broken)
    run_hook(monkeypatch, capsys, "UserPromptSubmit", base(ws))
    out = run_hook(monkeypatch, capsys, "PreToolUse", {**base(ws), "tool_name": "Edit",
                   "tool_input": {"file_path": str(env.code / "acme-web" / "README.md")}})
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    run_hook(monkeypatch, capsys, "Stop", base(ws))
    assert status(ws).status == db.YOUR_TURN
    log = hook_log(env)
    assert "PreToolUse: plugins: OSError: plugins dir unreadable" in log
    assert "Stop: plugins: OSError: plugins dir unreadable" in log


def test_plugin_prints_never_reach_the_hook_output(ws, pdir, monkeypatch, capsys):
    (pdir / "loud").mkdir()
    (pdir / "loud" / "plugin.toml").write_text(
        'name = "loud"\ntitle = "Loud"\nversion = "1"\napi = 1\nhooks = ["PreToolUse", "SessionStart"]\n')
    (pdir / "loud" / "plugin.py").write_text(
        "from agentmgr.plugin import HookResult\nprint('loud: imported')\n\n\n"
        "def deny(ctx):\n    print('loud: denying')\n    return HookResult(deny='loud says no')\n\n\n"
        "def section(ws):\n    print('loud: section')\n    return '## Loud'\n\n\n"
        "def register(api):\n    api.hook('PreToolUse', deny)\n    api.hook('SessionStart', lambda ctx: None)\n"
        "    api.claude_md(section)\n")
    plugins.reset()  # a hook process starts fresh
    out = run_hook(monkeypatch, capsys, "PreToolUse",
                   {**base(ws), "tool_name": "Read", "tool_input": {"file_path": str(ws.path / "TASK.md")}})
    assert json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] == "loud says no"

    plugins.reset()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({**base(ws), "source": "clear"})))  # regenerates
    hooks.main("SessionStart")
    captured = capsys.readouterr()
    assert "loud:" not in captured.out and "acme-web/: on ws-1-x-y" in captured.out
    assert "loud: imported" in captured.err and "loud: section" in captured.err


def test_a_result_that_is_not_a_hook_result_is_an_error(ws, env, pdir, monkeypatch, capsys):
    (pdir / "odd").mkdir()
    (pdir / "odd" / "plugin.toml").write_text(
        'name = "odd"\ntitle = "Odd"\nversion = "1"\napi = 1\nhooks = ["PostToolUse"]\n')
    (pdir / "odd" / "plugin.py").write_text("def register(api):\n    api.hook('PostToolUse', lambda ctx: 5)\n")
    plugins.reset()
    assert run_hook(monkeypatch, capsys, "PostToolUse", {**base(ws), "tool_name": "Read"}) == ""
    assert "plugin odd: hook PostToolUse: returned 5, not a HookResult of text fields" in hook_log(env)
    assert [(e.name, e.what) for e in plugins.persisted_errors()] == [("odd", "hook PostToolUse")]
