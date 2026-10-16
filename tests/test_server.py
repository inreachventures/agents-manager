import io
import json
import subprocess

import pytest

from agentmgr import db, server, tmux
from agentmgr import workstream as ws_mod


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("agentmgr.gh.available", lambda: False)
    monkeypatch.setattr("agentmgr.fetcher.fetch_stale", lambda *a, **k: {})


def ask(srv, method, **params):
    out = srv.out
    out.seek(0)
    out.truncate()
    srv.call(json.dumps({"id": 7, "method": method, "params": params}))
    [reply] = [json.loads(line) for line in out.getvalue().splitlines()]
    assert reply["id"] == 7
    return reply


def test_state_lists_workstreams_with_their_repos(env):
    ws = ws_mod.new(launch=False, ticket="PROJ-1", name="dark mode", repo_queries=["web"])
    gone = ws_mod.new(launch=False, name="old work")
    ws_mod.archive(gone.key)
    (ws.path / "acme-web" / "new.txt").write_text("x\n")

    data = server.state()
    [row] = data["workstreams"]
    assert (row["key"], row["label"], row["status"], row["intake"]) == ("PROJ-1", "PROJ-1 dark mode", "stopped", True)
    [repo] = row["repos"]
    assert (repo["repo"], repo["branch"], repo["state"], repo["tone"]) == (
        "acme-web", "proj-1-dark-mode", "✎ 1 uncommitted", "yellow")
    assert [r["key"] for r in data["archived"]] == [gone.key]
    assert data["origin"] == {"repos": 1, "checked_at": None}


def test_requests_go_through_the_workstream_service(env):
    srv = server.Server(io.StringIO())
    ws = ws_mod.new(launch=False, name="ship it", repo_queries=["web"])
    wt = ws.path / "acme-web"

    assert "acme-ml-pipeline" in ask(srv, "repos")["result"]["repos"]
    added = ask(srv, "add_repo", key=ws.key, repo="ml pipeline")["result"]
    assert (added["repo"], added["branch"], added["created_branch"]) == ("acme-ml-pipeline", "ws-1-ship-it", True)
    assert (ws.path / "acme-ml-pipeline").is_dir()

    (wt / "wip.txt").write_text("wip\n")
    diff = ask(srv, "diff", key=ws.key)["result"]
    assert [(r["repo"], [f["path"] for f in r["files"]]) for r in diff["repos"]] == [
        ("acme-web", ["wip.txt"]), ("acme-ml-pipeline", [])]

    report = ask(srv, "archive_report", key=ws.key)["result"]
    assert report["blocked"] and report["repos"][0]["blockers"] == ["1 uncommitted change(s)"]
    assert "refusing to archive" in ask(srv, "archive", key=ws.key)["error"]  # same guard as the CLI
    assert wt.exists()

    (wt / "wip.txt").unlink()
    assert ask(srv, "archive", key=ws.key)["result"]["log"][-1].startswith(f"{ws.key} archived")
    assert not wt.exists() and db.get_workstream(db.connect(), ws.key).status == db.ARCHIVED
    assert "error" in ask(srv, "diff", key=ws.key)
    assert ask(srv, "unarchive", key=ws.key)["result"]["log"]
    assert wt.exists()
    assert ask(srv, "nope")["error"] == "LookupError: unknown method 'nope'"


def test_serve_answers_each_line_and_pushes_state(env):
    ws_mod.new(launch=False, name="one")
    out = io.StringIO()
    srv = server.Server(out)
    srv.serve(io.StringIO('{"id": 1, "method": "state"}\n\n{"id": 2, "method": "repos"}\n'))
    msgs = [json.loads(line) for line in out.getvalue().splitlines()]
    replies = {m["id"]: m for m in msgs if "id" in m}
    assert set(replies) == {1, 2}
    assert [w["key"] for w in replies[1]["result"]["workstreams"]] == ["WS-1"]


@pytest.mark.skipif(not tmux.available(), reason="needs tmux")
def test_app_views_share_the_session_and_go_with_it(env):
    try:
        tmux.new_session("WS-1", env.root, ["sleep", "600"])
        view = tmux.ensure_view("WS-1")
        assert view == "WS-1_app" and tmux.ensure_view("WS-1") == view
        assert tmux.session_option(view, "status") == "off"
        pane = tmux.run("display-message", "-p", "-t", "=WS-1:", "#{pane_id}").stdout
        assert tmux.run("display-message", "-p", "-t", "=WS-1_app:", "#{pane_id}").stdout == pane  # same Claude
        tmux.set_window_option("WS-1", "@wm_prompt", "fix the tests")  # the prompt bar shows in both
        assert tmux.run("display-message", "-p", "-t", "=WS-1_app:", "#{@wm_prompt}").stdout.strip() == "fix the tests"
        argv = tmux.attach_command(view)
        assert argv[-2:] == ["-t", "=WS-1_app"] and "-u" in argv

        assert not tmux.watched("WS-1")
        tmux.run("rename-session", "-t", "=WS-1_app", "renamed")  # C-b $ in the app
        tmux.kill_session("WS-1")
        assert not tmux.sessions() & {"WS-1", "WS-1_app", "renamed"}  # the whole group goes

        tmux.new_session("WS-2", env.root, ["sleep", "600"])
        tmux.ensure_view("WS-2")
        tmux.run("kill-session", "-t", "=WS-2")  # as an older wm would: the view keeps the window alive
        assert "WS-2_app" in tmux.sessions()
        tmux.prune_views()
        assert "WS-2_app" not in tmux.sessions()

        tmux.new_session("WS-3", env.root, ["sleep", "600"])
        tmux.ensure_view("WS-3")
        tmux.run("kill-session", "-t", "=WS-3")
        tmux.new_session("WS-3", env.root, ["sleep", "600"])  # started again before the stale view was pruned
        tmux.ensure_view("WS-3")
        pane = tmux.run("display-message", "-p", "-t", "=WS-3:", "#{pane_id}").stdout
        assert tmux.run("display-message", "-p", "-t", "=WS-3_app:", "#{pane_id}").stdout == pane
    finally:
        tmux.run("kill-server", check=False)


@pytest.mark.skipif(not tmux.available(), reason="needs tmux")
def test_a_server_started_by_an_older_wm_gets_the_app_aware_bindings(env):
    from agentmgr.config import get_config

    socket = get_config().tmux_socket
    try:  # started without wm's config, as by a wm from before the app
        subprocess.run(["tmux", "-L", socket, "-f", "/dev/null", "new-session", "-d", "-s", "WS-1", "sleep 600"],
                       check=True)
        tmux.reload_conf()
        [h] = [line for line in tmux.run("list-keys", "-T", "prefix").stdout.splitlines() if line.split()[3] == "h"]
        assert "*_app" in h and "switch-client" in h
    finally:
        tmux.run("kill-server", check=False)


def test_attach_refuses_a_stopped_session(env):
    srv = server.Server(io.StringIO())
    ws = ws_mod.new(launch=False, name="not running")
    assert "is stopped" in ask(srv, "attach", key=ws.key)["error"]
    assert ask(srv, "switch", key=ws.key, pid=1)["result"] == {"switched": False}


@pytest.mark.skipif(not tmux.available(), reason="needs tmux")
def test_the_app_counts_as_watching_only_while_in_front(env, monkeypatch):
    try:
        tmux.new_session("WS-1", env.root, ["sleep", "600"])
        view = tmux.ensure_view("WS-1")
        monkeypatch.setattr(tmux, "client_sessions", lambda: {4242: view})  # the app's client shows WS-1
        srv = server.Server(io.StringIO())
        ask(srv, "focus", active=True)
        assert tmux.watched("WS-1")
        ask(srv, "focus", active=False)
        assert not tmux.watched("WS-1")
        monkeypatch.setattr(tmux, "client_sessions", lambda: {4242: view, 4343: "WS-1"})  # and a terminal does
        assert tmux.watched("WS-1")
    finally:
        tmux.run("kill-server", check=False)


def test_changes_to_one_workstream_wait_for_each_other(env, monkeypatch):
    import threading
    import time

    srv = server.Server(io.StringIO())
    ws = ws_mod.new(launch=False, name="one change only", repo_queries=["web"])
    order = []

    def slow_archive(key, force=False):
        order.append("archive starts")
        time.sleep(0.5)
        order.append("archive ends")
        return ["archived"]

    def add_repo(*args, **kwargs):
        order.append("add repo")
        raise ws_mod.WorkstreamError("archived")

    monkeypatch.setattr(ws_mod, "archive", slow_archive)
    monkeypatch.setattr(ws_mod, "add_repo", add_repo)
    first = threading.Thread(target=srv.call, args=(json.dumps({"id": 1, "method": "archive",
                                                                "params": {"key": ws.key}}),))
    first.start()
    time.sleep(0.1)
    srv.call(json.dumps({"id": 2, "method": "add_repo", "params": {"key": ws.key, "repo": "ml"}}))
    first.join()
    assert order == ["archive starts", "archive ends", "add repo"]
