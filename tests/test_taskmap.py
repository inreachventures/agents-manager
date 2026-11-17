import json
import shutil
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
    taskmap.write_drawing(ws, taskmap.Drawing(token=token, pid=None, started_at=time.time(), snapshot=snap))
    return snap


def draft_doc(prefix="acme-web"):
    """The PR Lens example with every lane id prefixed by a repo name."""
    doc = json.loads(EXAMPLE.read_text())
    ids = {lane["id"]: f"{prefix}/{lane['id']}" for lane in doc["lanes"]}
    for lane in doc["lanes"]:
        lane["id"] = ids[lane["id"]]
    for node in doc["nodes"]:
        node["lane"] = ids[node["lane"]]

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
