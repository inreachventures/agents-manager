"""Handler errors outlive the process that saw them: hooks, the app-server and commands each run in their own process,
so they are kept in the wm home for `wm doctor` and the app to show."""

import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentmgr import cli, config, db, plugins, server
from agentmgr import workstream as ws_mod

FIXTURE = Path(__file__).parent / "fixtures" / "plugins" / "demo"


@pytest.fixture(autouse=True)
def fresh_plugins(monkeypatch):
    monkeypatch.setattr("agentmgr.gh.available", lambda: False)
    monkeypatch.setattr("agentmgr.fetcher.fetch_stale", lambda *a, **k: {})
    plugins.reset()
    yield
    plugins.reset()
    plugins.set_logger(plugins.stderr_logger)


@pytest.fixture
def logged():
    lines: list[str] = []
    plugins.set_logger(lines.append)
    return lines


@pytest.fixture
def pdir(env, monkeypatch):
    d = env.root / "plugins"
    shutil.copytree(FIXTURE, d / "demo")
    for name in ("DEMO_RAISE", "DEMO_REPLACE", "DEMO_DOCTOR", "DEMO_MARKER", "DEMO_IMPORT_MARK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTMGR_PLUGINS_DIR", str(d))
    config.get_config.cache_clear()
    return d


def make_plugin(pdir, name, py):
    (pdir / name).mkdir()
    (pdir / name / "plugin.toml").write_text(f'name = "{name}"\ntitle = "{name.title()}"\nversion = "1"\napi = 1\n')
    (pdir / name / "plugin.py").write_text(py)


def errors_file() -> Path:
    return config.get_config().home / "plugin-errors.json"


def on_file() -> set[tuple[str, str, str]]:
    return {(e["plugin"], e["what"], e["reason"]) for e in json.loads(errors_file().read_text())}


def doctor() -> str:
    result = CliRunner().invoke(cli.app, ["doctor"])
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    return result.output


def demo():
    (lp,) = [lp for lp in plugins.loaded() if lp.manifest.name == "demo"]
    return lp


def wm(*args, stdin: str = "", **extra_env) -> subprocess.CompletedProcess:
    exe = Path(sys.executable).parent / "wm"
    return subprocess.run([str(exe), *args], input=stdin, capture_output=True, text=True,
                          env={**os.environ, **extra_env})


def test_hook_error_is_seen_by_a_later_doctor_and_cleared_by_success(pdir):
    ws = ws_mod.new(launch=False, name="x y", repo_queries=["web"])
    payload = json.dumps({"session_id": ws.session_id, "cwd": ws.folder, "tool_name": "Read"})
    assert wm("hook", "PostToolUse", stdin=payload, DEMO_RAISE="1").returncode == 0
    out = wm("doctor").stdout
    assert "Plugins" in out
    assert "demo: hook PostToolUse: RuntimeError: boom (just now)" in out

    assert wm("hook", "PostToolUse", stdin=payload).returncode == 0  # the handler succeeds
    assert "hook PostToolUse" not in wm("doctor").stdout
    assert on_file() == set()


def test_replacement_conflict_shows_in_doctor(pdir, monkeypatch):
    monkeypatch.setenv("DEMO_REPLACE", "1")
    make_plugin(pdir, "zzz", "def register(api):\n    api.replace_claude_md('progress_rule', lambda ws: '- zzz')\n")
    ws_mod.new(launch=False, name="x y")  # writes CLAUDE.md, in this process
    plugins.reset()  # `wm doctor` is another process
    assert "zzz: replace progress_rule: already replaced by demo" in doctor()


def test_conflict_cleared_once_the_other_plugin_is_gone(pdir, monkeypatch):
    monkeypatch.setenv("DEMO_REPLACE", "1")
    make_plugin(pdir, "zzz", "def register(api):\n    api.replace_claude_md('progress_rule', lambda ws: '- zzz')\n")
    ws = ws_mod.new(launch=False, name="x y")
    assert ("zzz", "replace progress_rule", "already replaced by demo") in on_file()
    shutil.rmtree(pdir / "demo")
    plugins.reset()
    ws_mod.regenerate(db.connect(), ws)
    assert "- zzz" in (ws.path / "CLAUDE.md").read_text()
    assert on_file() == set()


def test_doctor_shows_how_long_ago(pdir):
    errors_file().parent.mkdir(parents=True, exist_ok=True)
    errors_file().write_text(json.dumps([
        {"plugin": "demo", "what": "badge", "reason": "ZeroDivisionError: division by zero",
         "at": time.time() - 3 * 3600}]))
    assert "demo: badge: ZeroDivisionError: division by zero (3h ago)" in doctor()


def test_errors_of_plugins_no_longer_installed_are_dropped(pdir):
    errors_file().parent.mkdir(parents=True, exist_ok=True)
    errors_file().write_text(json.dumps([
        {"plugin": "gone", "what": "badge", "reason": "RuntimeError: x", "at": time.time()},
        {"plugin": "demo", "what": "badge", "reason": "RuntimeError: y", "at": time.time()}]))
    out = doctor()
    assert "gone" not in out and "demo: badge: RuntimeError: y" in out
    assert on_file() == {("demo", "badge", "RuntimeError: y")}


@pytest.mark.parametrize("text", ["{not json", '{"a": 1}', '[1, {"plugin": 2}]', ""])
def test_corrupt_errors_file_is_harmless(pdir, logged, text):
    errors_file().parent.mkdir(parents=True, exist_ok=True)
    errors_file().write_text(text)
    assert plugins.persisted_errors() == []
    assert "Plugins" not in doctor()
    plugins.reset()
    lp = demo()
    assert plugins.call(lp, "badge", lambda: "ok") == "ok"
    assert plugins.call(lp, "badge", lambda: 1 / 0) is None
    assert on_file() == {("demo", "badge", "ZeroDivisionError: division by zero")}


def test_unreadable_errors_file_never_raises(pdir, logged):
    errors_file().parent.mkdir(parents=True, exist_ok=True)
    errors_file().mkdir()  # can be neither read nor replaced
    lp = demo()
    assert plugins.call(lp, "badge", lambda: 1 / 0) is None
    assert plugins.call(lp, "badge", lambda: "ok") == "ok"
    assert plugins.persisted_errors() == []
    assert any("plugin-errors.json" in line for line in logged)


def test_success_reads_the_file_at_most_once_per_process(pdir, monkeypatch):
    reads = []
    real = plugins._read_errors_file
    monkeypatch.setattr(plugins, "_read_errors_file", lambda: reads.append(1) or real())
    lp = demo()
    for _ in range(5):
        assert plugins.call(lp, "badge", lambda: "ok") == "ok"
    assert len(reads) <= 1
    assert not errors_file().exists()  # nothing to clear: nothing written


def test_an_entry_another_process_wrote_is_cleared_by_success(pdir):
    errors_file().parent.mkdir(parents=True, exist_ok=True)
    errors_file().write_text(json.dumps([
        {"plugin": "demo", "what": "badge", "reason": "RuntimeError: x", "at": time.time()},
        {"plugin": "demo", "what": "doctor", "reason": "RuntimeError: y", "at": time.time()}]))
    assert plugins.call(demo(), "badge", lambda: "ok") == "ok"
    assert on_file() == {("demo", "doctor", "RuntimeError: y")}


def test_the_same_failure_again_is_not_rewritten_each_time(pdir, monkeypatch):
    writes = []
    real = plugins._write_errors_file
    monkeypatch.setattr(plugins, "_write_errors_file", lambda entries: writes.append(1) or real(entries))
    lp = demo()
    for _ in range(5):
        plugins.call(lp, "badge", lambda: 1 / 0)
    assert len(writes) == 1
    plugins.call(lp, "badge", lambda: None.x)  # a different reason is written
    assert len(writes) == 2
    assert on_file() == {("demo", "badge", "AttributeError: 'NoneType' object has no attribute 'x'")}


def test_wrong_result_is_recorded_once_not_cleared_first(pdir, monkeypatch):
    writes = []
    real = plugins._write_errors_file
    monkeypatch.setattr(plugins, "_write_errors_file", lambda entries: writes.append(1) or real(entries))
    lp = demo()
    for _ in range(3):
        assert plugins.call_text(lp, "claude_md", lambda: 42) is None
    assert len(writes) == 1
    assert on_file() == {("demo", "claude_md", "returned int, not text")}


def test_check_plugins_notices_each_new_error_once_per_run(pdir):
    make_plugin(pdir, "bad", "def register(api):\n    api.badge(lambda ws: 1 / 0)\n")
    ws_mod.new(launch=False, name="x y")
    ws_mod.new(launch=False, name="z w")
    srv = server.Server(io.StringIO())
    srv.push_state()
    srv.push_state()
    notices = [json.loads(line)["data"] for line in srv.out.getvalue().splitlines()
               if json.loads(line).get("event") == "notice"]
    assert notices == [{"level": "warning", "message": "Bad plugin: badge: ZeroDivisionError: division by zero"}]


def test_check_plugins_notices_errors_other_processes_recorded(pdir):
    errors_file().parent.mkdir(parents=True, exist_ok=True)
    errors_file().write_text(json.dumps([
        {"plugin": "demo", "what": "hook Stop", "reason": "RuntimeError: x", "at": time.time()}]))
    srv = server.Server(io.StringIO())
    srv.push_state()
    notices = [json.loads(line)["data"] for line in srv.out.getvalue().splitlines()
               if json.loads(line).get("event") == "notice"]
    assert notices == [{"level": "warning", "message": "Demo plugin: hook Stop: RuntimeError: x"}]


def test_the_app_server_writes_once_per_state_push(pdir, monkeypatch):
    make_plugin(pdir, "picky", "def register(api):\n"
                "    api.badge(lambda ws: 1 / 0 if ws.name.startswith('bad') else None)\n")
    for name in ("bad one", "good one", "bad two", "good two"):
        ws_mod.new(launch=False, name=name)
    writes = []
    real = plugins._write_errors_file
    monkeypatch.setattr(plugins, "_write_errors_file", lambda entries: writes.append(1) or real(entries))
    monkeypatch.setattr(plugins, "_batched", True)  # as server.main sets it
    srv = server.Server(io.StringIO())
    for _ in range(3):
        srv.push_state()
    assert len(writes) == 1
    assert on_file() == {("picky", "badge", "ZeroDivisionError: division by zero")}


def test_a_watch_whose_paths_raise_is_recorded_and_other_watches_go_on(pdir, tmp_path):
    make_plugin(pdir, "agen", "def paths():\n    yield 'x'\n    raise RuntimeError('mid-iteration')\n\n\n"
                "def register(api):\n    api.watch(lambda ws: paths())\n")
    ws = ws_mod.new(launch=False, name="x y")
    srv = server.Server(io.StringIO())
    srv.push_state()  # demo's watch is noted despite agen's
    srv.out.seek(0)
    srv.out.truncate()
    plugins.call(demo(), "x", lambda: None)  # demo loaded in this process
    (ws.path / ".wm" / "demo").mkdir(parents=True, exist_ok=True)
    (ws.path / ".wm" / "demo" / "notes.json").write_text("[]")
    srv.push_state()
    changed = [json.loads(line)["data"] for line in srv.out.getvalue().splitlines()
               if json.loads(line).get("event") == "plugin_changed"]
    assert changed == [{"plugin": "demo", "key": ws.key}]
    assert ("agen", "watch", "RuntimeError: mid-iteration") in {(e.name, e.what, e.reason)
                                                                for e in plugins.persisted_errors()}


@pytest.mark.parametrize("paths", [[3], [None], [1.5]])
def test_a_watch_path_that_is_not_a_path_is_a_wrong_result(pdir, paths):
    from agentmgr import server as srv_mod

    with pytest.raises(TypeError, match="not a list of paths"):
        srv_mod._stamp(paths)


def test_reasons_are_one_short_line(pdir, logged):
    lp = demo()
    plugins.call(lp, "badge", lambda: (_ for _ in ()).throw(RuntimeError("first\nsecond\n\tthird " + "x" * 500)))
    (e,) = plugins.persisted_errors()
    assert "\n" not in e.reason and len(e.reason) <= 300 and e.reason.endswith("…")
    assert e.reason.startswith("RuntimeError: first second third x")
    assert on_file() == {("demo", "badge", e.reason)}


def test_deeply_nested_errors_file_is_harmless(pdir):
    errors_file().parent.mkdir(parents=True, exist_ok=True)
    errors_file().write_text("[" * 100000 + "]" * 100000)
    assert plugins.persisted_errors() == []


def test_errors_file_with_a_timestamp_too_big_for_a_float_is_harmless(pdir):
    errors_file().parent.mkdir(parents=True, exist_ok=True)
    errors_file().write_text('[{"plugin": "demo", "what": "badge", "reason": "x", "at": 1' + "0" * 400 + "}]")
    assert plugins.persisted_errors() == []
