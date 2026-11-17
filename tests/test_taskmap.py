import json
import os
import shutil
import subprocess
import time

import pytest

from agentmgr import db, taskmap
from agentmgr import workstream as ws_mod

from .conftest import sh

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
EXAMPLE = taskmap.PRLENS / "references" / "example.graph.json"


def two_repos(env):
    ws = ws_mod.new(launch=False, name="map", repo_queries=["web", "ml pipeline"])
    return ws, ws.path / "acme-web", ws.path / "acme-ml-pipeline"


def links(ws):
    return db.list_repos(db.connect(), ws.key)


def commit(wt, name):
    (wt / name).write_text(name)
    sh(wt, "git", "add", name)
    sh(wt, "git", "commit", "-q", "-m", name)


def start_drawing(ws, token="tok"):
    snap = taskmap.snapshot(db.connect(), ws)
    taskmap.write_drawing(ws, taskmap.Drawing(token=token, pid=None, pgid=None, started_at=time.time(),
                                              snapshot=snap))
    return snap


def draft_doc(prefix="acme-web"):
    """The PR Lens example with every lane id and file path prefixed by a repo name."""
    doc = json.loads(EXAMPLE.read_text())
    ids = {lane["id"]: f"{prefix}/{lane['id']}" for lane in doc["lanes"]}
    for lane in doc["lanes"]:
        lane["id"] = ids[lane["id"]]
    for node in doc["nodes"]:
        node["lane"] = ids[node["lane"]]
        for file in node.get("files", []):
            file["path"] = f"{prefix}/{file['path']}"

    def rename_scopes(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("lanes", "laneOrder") and isinstance(v, list) and all(isinstance(x, str) for x in v):
                    o[k] = [ids.get(x, x) for x in v]
                else:
                    rename_scopes(v)
        elif isinstance(o, list):
            for x in o:
                rename_scopes(x)

    rename_scopes(doc)
    return doc


def write_draft(tmp_path, doc):
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(doc))
    return path


def test_snapshot_marks_each_repo(env):
    ws, web, ml = two_repos(env)
    before = taskmap.snapshot(db.connect(), ws)
    assert set(before) == {"acme-web", "acme-ml-pipeline"}
    for mark in before.values():
        assert len(mark.head) == 40 and len(mark.digest) == 16 and mark.error is None
    (web / "new.txt").write_text("x\n")
    after = taskmap.snapshot(db.connect(), ws)
    assert after["acme-web"].digest != before["acme-web"].digest
    assert after["acme-ml-pipeline"] == before["acme-ml-pipeline"]


def test_snapshot_survives_missing_worktree(env):
    ws, web, ml = two_repos(env)
    shutil.rmtree(ml)
    snap = taskmap.snapshot(db.connect(), ws)
    assert snap["acme-ml-pipeline"].error
    assert snap["acme-ml-pipeline"].head == ""
    assert snap["acme-web"].error is None and len(snap["acme-web"].head) == 40


def test_freshness_reasons(env):
    ws, web, ml = two_repos(env)
    saved = taskmap.snapshot(db.connect(), ws)
    assert taskmap.freshness(ws.key, saved, saved, links(ws)) == []

    commit(web, "a.txt")
    commit(web, "b.txt")
    (ml / "README.md").write_text("changed\n")
    ws_mod.add_repo(ws.key, "release tools")
    current = taskmap.snapshot(db.connect(), ws)
    got = taskmap.freshness(ws.key, saved, current, links(ws))
    assert got == [{"repo": "acme-web", "reason": "2 new commits"},
                   {"repo": "acme-ml-pipeline", "reason": "uncommitted changes"},
                   {"repo": "release-tools", "reason": "attached since the map was drawn"}]

    one = taskmap.snapshot(db.connect(), ws)
    sh(web, "git", "commit", "-q", "--amend", "-m", "reworded")
    amended = taskmap.snapshot(db.connect(), ws)
    reasons = {r["repo"]: r["reason"] for r in taskmap.freshness(ws.key, one, amended, links(ws))}
    assert reasons == {"acme-web": "rebased"}

    only_ml = [lk for lk in links(ws) if lk.repo == "acme-ml-pipeline"]
    assert [r["repo"] for r in taskmap.freshness(ws.key, saved, current, only_ml)] == ["acme-ml-pipeline"]


def test_freshness_unavailable(env):
    ws, web, ml = two_repos(env)
    saved = taskmap.snapshot(db.connect(), ws)
    shutil.rmtree(ml)
    current = taskmap.snapshot(db.connect(), ws)
    assert taskmap.freshness(ws.key, saved, current, links(ws)) == [
        {"repo": "acme-ml-pipeline", "reason": "unavailable"}]


def test_save_rejects_wrong_token(env, tmp_path):
    ws, *_ = two_repos(env)
    start_drawing(ws)
    with pytest.raises(taskmap.MapError, match="token"):
        taskmap.save(ws.key, "other", write_draft(tmp_path, draft_doc()))
    assert taskmap.load(ws) is None


def test_save_rejects_unprefixed_lane(env, tmp_path):
    ws, *_ = two_repos(env)
    start_drawing(ws)
    with pytest.raises(taskmap.MapError, match="'web'"):
        taskmap.save(ws.key, "tok", write_draft(tmp_path, json.loads(EXAMPLE.read_text())))


def test_save_rejects_repo_relative_file_paths(env, tmp_path):
    """A card's files must be workstream-relative (`<repo>/...`), or clicking it can never open its diff: the PR Lens
    examples use repo-relative paths, so a drawer copying them is told which paths to fix."""
    ws, *_ = two_repos(env)
    start_drawing(ws)
    doc = draft_doc()
    doc["nodes"][1]["files"][0]["path"] = "app/api/broadcasts/queue/route.ts"
    doc["dataModel"] = {"stores": [{"id": "s", "label": "S", "platform": "Postgres"}], "tables": [
        {"id": "t", "label": "T", "store": "s", "delta": "added", "columns": [{"name": "id", "type": "uuid"}],
         "files": [{"path": "acme-ml-pipeline/db/t.sql"}, {"path": "migrations/0001.sql"}]}]}
    with pytest.raises(taskmap.MapError) as err:
        taskmap.save(ws.key, "tok", write_draft(tmp_path, doc))
    problems = str(err.value).splitlines()
    assert [p for p in problems if "files" in p] == [
        "nodes[1].files[0].path 'app/api/broadcasts/queue/route.ts' must start with the folder of a repo in this "
        "map, e.g. acme-web/app/api/broadcasts/queue/route.ts",
        "dataModel.tables[0].files[1].path 'migrations/0001.sql' must start with the folder of a repo in this map, "
        "e.g. acme-web/migrations/0001.sql"]
    assert taskmap.load(ws) is None


@needs_node
def test_save_rejects_invalid_doc(env, tmp_path):
    ws, *_ = two_repos(env)
    start_drawing(ws)
    doc = draft_doc()
    doc["edges"][0]["to"] = "nowhere"
    with pytest.raises(taskmap.MapError, match=doc["edges"][0]["id"]):
        taskmap.save(ws.key, "tok", write_draft(tmp_path, doc))
    assert taskmap.load(ws) is None


@needs_node
def test_save_accepts_lanes_of_repo_removed_since_start(env, tmp_path):
    ws, *_ = two_repos(env)
    start_drawing(ws)
    ws_mod.remove_repo(ws.key, "acme-web", force=True)
    taskmap.save(ws.key, "tok", write_draft(tmp_path, draft_doc()))
    assert taskmap.load(ws) is not None


@needs_node
def test_save_writes_map_and_overwrites_provenance(env, tmp_path):
    ws, *_ = two_repos(env)
    snap = start_drawing(ws)
    ws_mod.remove_repo(ws.key, "acme-web", force=True)
    taskmap.save(ws.key, "tok", write_draft(tmp_path, draft_doc()))
    saved = taskmap.load(ws)
    prov = saved["doc"]["provenance"]
    assert prov["repo"] == {"owner": "wm", "name": ws.key}
    assert prov["head"]["sha"] == snap["acme-web"].head
    assert saved["snapshot"] == snap and saved["token"] == "tok"
    assert taskmap.read_drawing(ws).saved is True


def test_read_drawing_ignores_wrong_shape(env):
    ws, *_ = two_repos(env)
    taskmap.map_dir(ws).mkdir(parents=True, exist_ok=True)
    for body in ('{"token": "t"}', "[1, 2]", '{"token": "t", "pid": 1, "started_at": 0, "snapshot": {"a": 5}}'):
        (taskmap.map_dir(ws) / "drawing.json").write_text(body)
        assert taskmap.read_drawing(ws) is None


@needs_node
def test_save_reports_a_validator_timeout(env, tmp_path, monkeypatch):
    ws, *_ = two_repos(env)
    start_drawing(ws)

    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(taskmap.subprocess, "run", hang)
    with pytest.raises(taskmap.MapError, match="did not finish"):
        taskmap.save(ws.key, "tok", write_draft(tmp_path, draft_doc()))


def test_save_leaves_the_thread_connection_usable(env, tmp_path):
    ws, *_ = two_repos(env)
    start_drawing(ws)
    with pytest.raises(taskmap.MapError):
        taskmap.save(ws.key, "wrong", write_draft(tmp_path, draft_doc()))
    assert [w.key for w in db.list_workstreams(db.connect())] == [ws.key]


# ---------------------------------------------------------------------------- drawing process


class Fakes:
    """`_spawn`, `_alive` and `_kill` replaced: spawns are recorded, every pid is alive until marked dead."""

    def __init__(self, monkeypatch, node=True):
        self.calls, self.dead, self.killed = [], set(), []
        real_which = shutil.which

        def which(cmd, *args, **kwargs):
            if cmd == "node":
                return "/fake/bin/node" if node else None
            if cmd == "claude":
                return "/fake/bin/claude"
            return real_which(cmd, *args, **kwargs)

        def spawn(argv, cwd, env, log):
            self.calls.append({"argv": argv, "cwd": cwd, "env": env, "log": log})
            return 4200 + len(self.calls), 4100 + len(self.calls)

        monkeypatch.setattr(taskmap.shutil, "which", which)
        monkeypatch.setattr(taskmap, "_spawn", spawn)
        monkeypatch.setattr(taskmap, "_alive", lambda pid, pgid: pid not in self.dead and pgid == pid - 100)
        monkeypatch.setattr(taskmap, "_kill", lambda pid, pgid: self.killed.append(pid))


def allowed(argv):
    return argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]


def test_start_drawing_spawns_with_flag_and_allowed_tools(env, monkeypatch):
    from agentmgr import tmux

    ws, web, ml = two_repos(env)
    fakes = Fakes(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert taskmap.start_drawing(db.connect(), ws) == "started"
    [call] = fakes.calls
    assert call["env"][taskmap.DRAWING_ENV] == "1"
    assert "CLAUDECODE" not in call["env"]
    assert call["cwd"] == ws.path and call["log"] == taskmap.map_dir(ws) / "last-run.log"
    assert call["argv"][:3] == ["/fake/bin/claude", "-p", "Draw this workstream's task map: follow .wm/map/brief.md"]
    rules = allowed(call["argv"])
    assert not {"Read", "Glob", "Grep"} & set(rules)
    drawing = taskmap.read_drawing(ws)
    for repo, wt in (("acme-web", web), ("acme-ml-pipeline", ml)):
        fork = drawing.snapshot[repo].fork
        # only the diff from the fork point: a first argument that is a commit keeps git diff from reading two
        # files outside the repo (its implicit --no-index)
        assert f"Bash(git -C {wt} diff --find-renames {fork} *)" in rules
        assert f"Bash(git -C {wt} diff *)" not in rules
        assert f"`git -C {wt} diff --find-renames {fork}`" in (taskmap.map_dir(ws) / "brief.md").read_text()
    assert "Bash(git * --no-index*)" in call["argv"][call["argv"].index("--disallowedTools"):]
    assert f"Bash({tmux.wm_executable()} map save *)" in rules
    draft = taskmap.map_dir(ws) / "draft.json"
    assert f"Edit(/{draft})" in rules
    brief = (taskmap.map_dir(ws) / "brief.md").read_text()
    assert str(draft) in brief and "0.3.1" in brief and "acme-web/" in brief
    assert "`acme-web/src/app.ts`, never `src/app.ts`" in brief
    drawing = taskmap.read_drawing(ws)
    assert drawing.pid == 4201 and drawing.pgid == 4101 and drawing.token in brief
    assert set(drawing.snapshot) == {"acme-web", "acme-ml-pipeline"}


def test_second_request_is_queued_then_rerun_once(env, monkeypatch):
    ws, web, ml = two_repos(env)
    fakes = Fakes(monkeypatch)
    conn = db.connect()
    assert taskmap.start_drawing(conn, ws) == "started"
    assert taskmap.start_drawing(conn, ws) == "queued"
    assert taskmap.read_drawing(ws).requested_again is True
    fakes.dead.add(4201)
    commit(web, "a.txt")
    taskmap.reap(conn, ws)
    assert len(fakes.calls) == 2
    drawing = taskmap.read_drawing(ws)
    assert drawing.pid == 4202 and drawing.requested_again is False
    assert drawing.snapshot["acme-web"].head == sh(web, "git", "rev-parse", "HEAD")


def test_queued_request_not_rerun_when_nothing_moved(env, monkeypatch):
    ws, *_ = two_repos(env)
    fakes = Fakes(monkeypatch)
    conn = db.connect()
    taskmap.start_drawing(conn, ws)
    taskmap.start_drawing(conn, ws)
    fakes.dead.add(4201)
    taskmap.reap(conn, ws)
    assert len(fakes.calls) == 1
    assert taskmap.read_drawing(ws) is None


def test_start_after_a_queued_drawing_ended_draws_once(env, monkeypatch):
    """The rerun a finished drawing owes already sees the latest state, so the request that noticed it is not
    queued behind it."""
    ws, web, ml = two_repos(env)
    fakes = Fakes(monkeypatch)
    conn = db.connect()
    taskmap.start_drawing(conn, ws)
    taskmap.start_drawing(conn, ws)
    fakes.dead.add(4201)
    commit(web, "a.txt")
    assert taskmap.start_drawing(conn, ws) == "started"
    assert len(fakes.calls) == 2
    assert taskmap.read_drawing(ws).requested_again is False


def test_exit_without_save_records_failure_with_log_tail(env, monkeypatch):
    ws, *_ = two_repos(env)
    fakes = Fakes(monkeypatch)
    conn = db.connect()
    taskmap.start_drawing(conn, ws)
    lines = [f"line {i}" for i in range(30)]
    (taskmap.map_dir(ws) / "last-run.log").write_text("\n".join(lines) + "\n")
    taskmap.reap(conn, ws)
    assert taskmap.read_drawing(ws) is not None  # still running
    fakes.dead.add(4201)
    taskmap.reap(conn, ws)
    assert taskmap.read_drawing(ws) is None
    failure = taskmap.last_failure(ws)
    assert failure["reason"] == "\n".join(lines[-20:]) and failure["at"] <= time.time()


def test_exit_after_save_records_no_failure(env, monkeypatch):
    ws, *_ = two_repos(env)
    fakes = Fakes(monkeypatch)
    conn = db.connect()
    taskmap.start_drawing(conn, ws)
    drawing = taskmap.read_drawing(ws)
    drawing.saved = True
    taskmap.write_drawing(ws, drawing)
    fakes.dead.add(4201)
    taskmap.reap(conn, ws)
    assert taskmap.read_drawing(ws) is None and taskmap.last_failure(ws) is None


def test_timeout_kills_and_records(env, monkeypatch):
    ws, *_ = two_repos(env)
    fakes = Fakes(monkeypatch)
    conn = db.connect()
    taskmap.start_drawing(conn, ws)
    later = taskmap.read_drawing(ws).started_at + taskmap.DRAWING_TIMEOUT + 1
    monkeypatch.setattr(taskmap.time, "time", lambda: later)
    taskmap.reap(conn, ws)
    assert fakes.killed == [4201]
    assert taskmap.read_drawing(ws) is None
    assert taskmap.last_failure(ws)["reason"] == "timed out after 15 minutes"


def test_no_node_records_failure(env, monkeypatch):
    ws, *_ = two_repos(env)
    fakes = Fakes(monkeypatch, node=False)
    assert taskmap.start_drawing(db.connect(), ws) == "no-node"
    assert fakes.calls == [] and taskmap.read_drawing(ws) is None
    assert taskmap.last_failure(ws)["reason"] == "The task map needs Node.js"


def test_archive_cancels_drawing(env, monkeypatch):
    ws, *_ = two_repos(env)
    fakes = Fakes(monkeypatch)
    taskmap.start_drawing(db.connect(), ws)
    ws_mod.archive(ws.key, force=True)
    assert fakes.killed == [4201]
    assert taskmap.read_drawing(ws) is None


def test_brief_redraws_from_existing_map(env, monkeypatch):
    ws, *_ = two_repos(env)
    Fakes(monkeypatch)
    taskmap.map_dir(ws).mkdir(parents=True, exist_ok=True)
    (taskmap.map_dir(ws) / "map.json").write_text(json.dumps(
        {"doc": {"title": "t"}, "drawn_at": 1.0, "snapshot": {}, "token": "old"}))
    taskmap.start_drawing(db.connect(), ws)
    brief = (taskmap.map_dir(ws) / "brief.md").read_text()
    assert str(taskmap.map_dir(ws) / "map.json") in brief and "keep" in brief


def test_cli_map_save(env, tmp_path):
    from typer.testing import CliRunner

    from agentmgr.cli import app

    ws, *_ = two_repos(env)
    start_drawing(ws)
    result = CliRunner().invoke(app, ["map", "save", ws.key, "--token", "wrong",
                                      str(write_draft(tmp_path, draft_doc()))])
    assert result.exit_code == 1
    assert "token" in result.output


@needs_node
def test_cli_map_save_success_clears_failure(env, tmp_path):
    from typer.testing import CliRunner

    from agentmgr.cli import app

    ws, *_ = two_repos(env)
    start_drawing(ws)
    taskmap.map_dir(ws).mkdir(parents=True, exist_ok=True)
    (taskmap.map_dir(ws) / "failure.json").write_text(json.dumps({"at": 1.0, "reason": "earlier"}))
    result = CliRunner().invoke(app, ["map", "save", ws.key, "--token", "tok",
                                      str(write_draft(tmp_path, draft_doc()))])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "saved"
    assert taskmap.load(ws) is not None and taskmap.last_failure(ws) is None


def test_spawn_detaches_and_kill_stops_the_group(tmp_path):
    log = tmp_path / "run.log"
    pid, pgid = taskmap._spawn(["/bin/sh", "-c", "echo started; sleep 30"], tmp_path, dict(os.environ), log)
    assert pgid != os.getpgid(0) and os.getpgid(pid) == pgid
    deadline = time.time() + 5
    while "started" not in (log.read_text() if log.exists() else "") and time.time() < deadline:
        time.sleep(0.05)
    assert "started" in log.read_text()
    assert taskmap._alive(pid, pgid)
    taskmap._kill(pid, pgid)
    while taskmap._alive(pid, pgid) and time.time() < deadline:
        time.sleep(0.05)
    assert not taskmap._alive(pid, pgid)


def test_drawing_is_confined_to_the_brief(env, monkeypatch):
    """No settings, hooks or MCP servers of the user's, the workstream's or a repo's reach the drawing; its file
    tools read only the workstream folder and the vendored PR Lens folder; nothing prompts."""
    ws, *_ = two_repos(env)
    fakes = Fakes(monkeypatch)
    taskmap.start_drawing(db.connect(), ws)
    argv = fakes.calls[0]["argv"]

    def value(flag):
        return argv[argv.index(flag) + 1]

    assert "--restricted" in argv and "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert "--safe-mode" in argv  # no CLAUDE.md, skills, agents or auto memory: the brief is all it is told
    assert value("--permission-mode") == "dontAsk"
    assert value("--tools") == "Bash,Read,Edit,Write,Glob,Grep"
    assert json.loads(value("--settings")) == {"disableAllHooks": True,
                                               "permissions": {"blockReadsOutsideWorkingDirectories": True}}
    assert value("--add-dir") == str(taskmap.PRLENS)
    assert "--setting-sources" not in argv  # --restricted already loads none


def test_reused_pid_is_not_the_drawing(monkeypatch):
    """A pid now in another process group is someone else's process: not alive, and never signalled."""
    signalled = []
    monkeypatch.setattr(taskmap.os, "getpgid", lambda pid: 999 if pid else 1)
    monkeypatch.setattr(taskmap.os, "killpg", lambda pgid, sig: signalled.append(pgid))
    assert taskmap._alive(4201, 4101) is False
    taskmap._kill(4201, 4101)
    assert signalled == []
    assert taskmap._alive(4201, 999) is True
    taskmap._kill(4201, 999)
    assert signalled == [999]


def test_no_drawing_for_an_archived_workstream(env, monkeypatch):
    ws, *_ = two_repos(env)
    fakes = Fakes(monkeypatch)
    ws_mod.archive(ws.key, force=True)
    assert taskmap.start_drawing(db.connect(), ws) == "unavailable"
    assert fakes.calls == [] and not taskmap.map_dir(ws).exists()


def test_no_drawing_once_the_folder_is_gone(env, monkeypatch):
    ws, *_ = two_repos(env)
    fakes = Fakes(monkeypatch)
    shutil.rmtree(ws.path)
    assert taskmap.start_drawing(db.connect(), ws) == "unavailable"
    assert fakes.calls == [] and not ws.path.exists()
