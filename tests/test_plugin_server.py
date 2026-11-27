"""The app-server's side of plugins: listing them, their requests, badges, watched files, reloads and jobs."""

import io
import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from agentmgr import config, db, plugins, server, tmux
from agentmgr import workstream as ws_mod

FIXTURE = Path(__file__).parent / "fixtures" / "plugins" / "demo"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("agentmgr.gh.available", lambda: False)
    monkeypatch.setattr("agentmgr.fetcher.fetch_stale", lambda *a, **k: {})


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
    monkeypatch.delenv("DEMO_MARKER", raising=False)
    config.get_config.cache_clear()
    return d


def make_plugin(pdir, name, py):
    (pdir / name).mkdir()
    (pdir / name / "plugin.toml").write_text(f'name = "{name}"\ntitle = "{name.title()}"\nversion = "2"\napi = 1\n')
    (pdir / name / "plugin.py").write_text(py)


def ask(srv, rpc, **params):
    srv.out.seek(0)
    srv.out.truncate()
    srv.call(json.dumps({"id": 7, "method": rpc, "params": params}))
    [reply] = [json.loads(line) for line in srv.out.getvalue().splitlines()]
    assert reply["id"] == 7
    return reply


def events(srv, name=None) -> list:
    """The events sent since the last call (`name`: only those), and forget them."""
    msgs = [json.loads(line) for line in srv.out.getvalue().splitlines()]
    srv.out.seek(0)
    srv.out.truncate()
    return [m["data"] for m in msgs if "event" in m and (name is None or m["event"] == name)]


def test_plugins_lists_views(pdir):
    make_plugin(pdir, "angry", "def register(api):\n    raise RuntimeError('nope')\n")
    make_plugin(pdir, "later", "def register(api): pass\n")
    plugins.set_enabled("later", False)
    (pdir / "broken").mkdir()
    (pdir / "broken" / "plugin.toml").write_text("name = [")
    srv = server.Server(io.StringIO())
    page = (pdir / "demo" / "web").resolve()

    listed = {p["name"]: p for p in ask(srv, "plugins")["result"]["plugins"]}
    assert list(listed) == ["angry", "broken", "demo", "later"]
    assert listed["demo"] == {
        "name": "demo", "title": "Demo", "version": "0.1.0", "status": "on", "reason": None,
        "path": str(pdir / "demo"), "views": [
            {"id": "notes", "title": "Notes", "place": "panel", "page": str(page / "notes.html"), "shortcut": None},
            {"id": "board", "title": "Board", "place": "main", "page": str(page / "board.html"),
             "shortcut": "opt+cmd+d"}]}
    assert listed["angry"] == {"name": "angry", "title": "Angry", "version": "2", "status": "skipped",
                               "reason": "RuntimeError: nope", "path": str(pdir / "angry"), "views": []}
    assert listed["later"] == {"name": "later", "title": "Later", "version": "2", "status": "off", "reason": None,
                               "path": str(pdir / "later"), "views": []}
    broken = listed["broken"]
    assert (broken["title"], broken["version"], broken["status"]) == ("broken", "", "skipped")
    assert broken["reason"].startswith("plugin.toml is not valid TOML")
    assert not srv.wake.is_set()  # a read: no state push for it


def test_plugins_empty_without_a_plugins_folder(env):
    srv = server.Server(io.StringIO())
    assert ask(srv, "plugins")["result"] == {"plugins": []}
    ws_mod.new(launch=False, name="one")
    assert [w["badges"] for w in server.state({})["workstreams"]] == [[]]
    assert not [m for m in __import__("sys").modules if m.startswith(plugins.MODULE_PREFIX)]


def test_set_plugin(pdir):
    srv = server.Server(io.StringIO())
    assert ask(srv, "set_plugin", name="demo", on=False)["result"] == {}
    assert srv.wake.is_set()  # shows at once
    [demo] = ask(srv, "plugins")["result"]["plugins"]
    assert demo["status"] == "off"
    assert ask(srv, "set_plugin", name="demo", on=True)["result"] == {}
    assert ask(srv, "set_plugin", name="nope", on=False)["error"] == "no plugin named 'nope'"


def test_plugin_request_roundtrip(pdir):
    ws = ws_mod.new(launch=False, name="x y", repo_queries=["web"])
    srv = server.Server(io.StringIO())
    added = ask(srv, "plugin_request", plugin="demo", method="add_note", params={"key": ws.key, "text": "hello"})
    assert added == {"id": 7, "result": {}}
    notes = ask(srv, "plugin_request", plugin="demo", method="notes", params={"key": ws.key})
    assert notes["result"] == ["hello"]
    assert not srv.wake.is_set()  # plugin requests never wake the state loop


def test_plugin_request_errors(pdir, capsys):
    make_plugin(pdir, "odd", "import sys\ndef register(api):\n"
                "    api.request('quit', lambda: sys.exit(3))\n"
                "    api.request('thing', lambda: object())\n"
                "    api.request('fail', lambda: 1 / 0)\n")
    srv = server.Server(io.StringIO())

    def error(plugin, method, params):
        reply = ask(srv, "plugin_request", plugin=plugin, method=method, params=params)
        assert "result" not in reply
        return reply["error"]

    assert error("demo", "x", {}) == "plugin demo has no request 'x'"
    assert error("nope", "notes", {}) == "plugin nope has no request 'notes'"
    with pytest.raises(ws_mod.WorkstreamError) as missing:
        ws_mod.require(db.connect(), "WS-99")
    assert error("demo", "notes", {"key": "WS-99"}) == str(missing.value)  # PluginError: just its message
    capsys.readouterr()
    assert error("odd", "fail", {}) == "ZeroDivisionError: division by zero"
    assert "Traceback" in capsys.readouterr().err
    assert error("demo", "notes", {"nope": 1}).startswith("TypeError: ")
    assert error("odd", "quit", {}) == "SystemExit: 3"  # answered, and the server lives on
    assert error("odd", "thing", {}).startswith("TypeError: Object of type object is not JSON serializable")
    assert error("demo", "notes", ["WS-1"]) == "params must be an object"


def test_badges_in_state(pdir):
    make_plugin(pdir, "bad", "def register(api):\n    api.badge(lambda ws: 1 / 0)\n")
    make_plugin(pdir, "wrong", "def register(api):\n    api.badge(lambda ws: 'not a badge')\n")
    ws = ws_mod.new(launch=False, name="x y", repo_queries=["web"])
    gone = ws_mod.new(launch=False, name="old")
    ws_mod.archive(gone.key)
    srv = server.Server(io.StringIO())

    data = server.state({})
    assert [w["badges"] for w in data["workstreams"]] == [[]]
    assert [w["badges"] for w in data["archived"]] == [[]]
    ask(srv, "plugin_request", plugin="demo", method="add_note", params={"key": ws.key, "text": "one"})
    [row] = server.state({})["workstreams"]
    assert row["badges"] == [{"plugin": "demo", "text": "1", "tone": "blue", "help": "1 demo note"}]
    assert {(e.name, e.what) for e in plugins.errors()} == {("bad", "badge"), ("wrong", "badge")}


def test_watch_sends_plugin_changed(pdir):
    ws = ws_mod.new(launch=False, name="x y", repo_queries=["web"])
    other = ws_mod.new(launch=False, name="other")
    srv = server.Server(io.StringIO())
    srv.push_state()
    assert events(srv, "plugin_changed") == []  # first sighting
    ask(srv, "plugin_request", plugin="demo", method="add_note", params={"key": ws.key, "text": "one"})
    events(srv)
    srv.push_state()
    assert events(srv, "plugin_changed") == [{"plugin": "demo", "key": ws.key}]
    srv.push_state()
    assert events(srv, "plugin_changed") == []
    ask(srv, "plugin_request", plugin="demo", method="add_note", params={"key": other.key, "text": "two"})
    srv.push_state()
    assert events(srv, "plugin_changed") == [{"plugin": "demo", "key": other.key}]


def test_watch_failure_is_not_a_change(pdir, tmp_path):
    flag, watched = tmp_path / "fail", tmp_path / "w.txt"
    make_plugin(pdir, "flaky", "from pathlib import Path\ndef register(api):\n"
                f"    api.watch(lambda ws: 1 / 0 if Path({str(flag)!r}).exists() else [Path({str(watched)!r})])\n")
    make_plugin(pdir, "single", "def register(api):\n    api.watch(lambda ws: 'one/path')\n")
    ws_mod.new(launch=False, name="x y")
    srv = server.Server(io.StringIO())
    srv.push_state()
    flag.touch()
    srv.push_state()
    flag.unlink()
    srv.push_state()
    assert events(srv, "plugin_changed") == []
    errors = {(e.name, e.what): e.reason for e in plugins.errors()}
    assert errors[("single", "watch")] == "TypeError: watch returned str, not a list of paths"
    watched.write_text("x")
    srv.push_state()
    assert [e["plugin"] for e in events(srv, "plugin_changed")] == ["flaky"]


def test_fingerprint_change_regenerates_and_announces(pdir):
    ws = ws_mod.new(launch=False, name="x y", repo_queries=["web"])
    assert "## Demo notes" in (ws.path / "CLAUDE.md").read_text()
    srv = server.Server(io.StringIO())
    srv.push_state()
    assert events(srv, "plugins_changed") == []

    shutil.rmtree(pdir / "demo")
    srv.push_state()
    msgs = events(srv)
    assert {} in msgs and msgs.index({}) == 0  # plugins_changed, before the state that follows
    assert "## Demo notes" not in (ws.path / "CLAUDE.md").read_text()
    srv.push_state()
    assert events(srv, "plugins_changed") == []  # once per change
    assert ask(srv, "state")["result"]["workstreams"][0]["badges"] == []  # keeps serving


def test_failed_plugin_notice_once(pdir):
    make_plugin(pdir, "angry", "def register(api):\n    raise RuntimeError('nope')\n")
    make_plugin(pdir, "later", "def register(api): pass\n")
    plugins.set_enabled("later", False)
    (pdir / "broken").mkdir()
    (pdir / "broken" / "plugin.toml").write_text('name = "broken"\ntitle = "Broken"\nversion = "1"\napi = 9\n')
    srv = server.Server(io.StringIO())
    srv.push_state()
    assert events(srv, "notice") == [
        {"level": "warning", "message": "Angry plugin skipped: RuntimeError: nope"},
        {"level": "warning", "message": "broken plugin skipped: written for plugin API 9, wm has 1"},
    ]
    srv.push_state()
    assert events(srv, "notice") == []


def test_every_runs(pdir, monkeypatch, tmp_path):
    marker = tmp_path / "marker"
    marker.mkdir()
    monkeypatch.setenv("DEMO_MARKER", str(marker))
    make_plugin(pdir, "boom", "def register(api):\n    api.every(0.05, lambda: 1 / 0)\n")
    srv = server.Server(io.StringIO())
    r, w = os.pipe()
    stdin = os.fdopen(r)
    t = threading.Thread(target=srv.serve, args=(stdin,), daemon=True)
    t.start()
    try:
        ticks = marker / "ticks.txt"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (ticks.exists() and int(ticks.read_text() or 0) >= 2):
            time.sleep(0.05)
        assert int(ticks.read_text()) >= 2  # every 1s, and still going despite the job next to it failing
        assert ("boom", "every 0.05s") in {(e.name, e.what) for e in plugins.errors()}
    finally:
        os.close(w)
        t.join(10)
    assert not t.is_alive()


def test_send_refuses_stopped(pdir, monkeypatch):
    ws = ws_mod.new(launch=False, name="x y")
    srv = server.Server(io.StringIO())
    assert ask(srv, "send", key=ws.key, text="hi")["error"] == f"{ws.key} is stopped; resume it first"

    sent = []
    monkeypatch.setattr(ws_mod, "session_alive", lambda w: True)
    monkeypatch.setattr(tmux, "send_text", lambda name, text: sent.append((name, text)))
    srv.wake.clear()
    assert ask(srv, "send", key=ws.key.lower(), text="hi")["result"] == {}
    assert sent == [(ws.key, "hi")]
    assert srv.wake.is_set()
