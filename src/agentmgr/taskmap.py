"""The task map: a PR Lens graph of what a workstream changes across its repos, and how to tell it has gone stale.

Map files live under `<workstream folder>/.wm/map/`, never inside a repo: `map.json` is the saved map, `drawing.json`
the drawing in progress, `lock` serialises changes to the drawing state."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.resources
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import db, diffs, gitops, workstream

PRLENS = importlib.resources.files("agentmgr") / "data" / "prlens"
NO_SHA = "0" * 40
VALIDATE_TIMEOUT = 30


class MapError(RuntimeError):
    pass


@dataclass
class RepoMark:
    """Where one repo stood when the map was drawn (or now): its head, fork point and a digest of its diff."""
    head: str
    fork: str
    digest: str
    error: str | None = None


@dataclass
class Drawing:
    token: str
    pid: int | None
    started_at: float
    snapshot: dict[str, RepoMark]
    requested_again: bool = False
    saved: bool = False


def map_dir(ws: db.Workstream) -> Path:
    return ws.path / ".wm" / "map"


@contextlib.contextmanager
def locked(ws: db.Workstream):
    """Exclusive lock for every read-modify-write of the drawing state. Not reentrant."""
    map_dir(ws).mkdir(parents=True, exist_ok=True)
    with open(map_dir(ws) / "lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def diff_digest(diff: dict) -> str:
    """Short fingerprint of a `diffs.repo_diff` result; a pure function of it, so callers may cache it."""
    return hashlib.sha1(json.dumps(diff, sort_keys=True).encode()).hexdigest()[:16]


def repo_mark(link: db.RepoLink) -> RepoMark:
    wt = Path(link.worktree_path)
    try:
        if not wt.is_dir():
            raise gitops.GitError(f"worktree {wt} is missing")
        head = gitops.git(wt, "rev-parse", "HEAD")
        fork = gitops.git(wt, "merge-base", link.base, "HEAD") if gitops.ref_exists(wt, link.base) else head
        return RepoMark(head=head, fork=fork, digest=diff_digest(diffs.repo_diff(wt, link.base)))
    except (gitops.GitError, OSError) as e:
        return RepoMark(head="", fork="", digest="", error=str(e))


def snapshot(conn, ws: db.Workstream) -> dict[str, RepoMark]:
    return {link.repo: repo_mark(link) for link in db.list_repos(conn, ws.key)}


def freshness(ws_key: str, saved: dict[str, RepoMark], current: dict[str, RepoMark],
              links: list[db.RepoLink]) -> list[dict]:
    """One {"repo", "reason"} per repo whose changes differ from what the map was drawn from; [] = up to date."""
    stale = []
    for link in links:
        now = current.get(link.repo)
        if now is None:
            continue
        then = saved.get(link.repo)
        wt = Path(link.worktree_path)
        if now.error or (then and then.error):
            reason = "unavailable"
        elif then is None:
            reason = "attached since the map was drawn"
        elif then.head != now.head:
            if gitops.ok(wt, "merge-base", "--is-ancestor", then.head, now.head):
                n = gitops.count_commits(wt, f"{then.head}..{now.head}")
                reason = f"{n} new commit{'' if n == 1 else 's'}"
            else:
                reason = "rebased"
        elif then.digest != now.digest:
            reason = "uncommitted changes"
        else:
            continue
        stale.append({"repo": link.repo, "reason": reason})
    return stale


def _marks(raw: dict) -> dict[str, RepoMark]:
    return {repo: RepoMark(**m) for repo, m in raw.items()}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_drawing(ws: db.Workstream) -> Drawing | None:
    try:
        raw = json.loads((map_dir(ws) / "drawing.json").read_text())
        return Drawing(**{**raw, "snapshot": _marks(raw["snapshot"])})
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def write_drawing(ws: db.Workstream, d: Drawing | None) -> None:
    path = map_dir(ws) / "drawing.json"
    if d is None:
        path.unlink(missing_ok=True)
    else:
        _write_json(path, asdict(d))


def load(ws: db.Workstream) -> dict | None:
    try:
        raw = json.loads((map_dir(ws) / "map.json").read_text())
        return {"doc": raw["doc"], "drawn_at": raw["drawn_at"], "snapshot": _marks(raw["snapshot"]),
                "token": raw["token"]}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _validate(doc: dict) -> list[str]:
    node = shutil.which("node")
    if node is None:
        raise MapError("node is not installed, so the map cannot be validated")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "map.graph.json"
        path.write_text(json.dumps(doc))
        try:
            proc = subprocess.run([node, str(PRLENS / "prlens-validate.mjs"), str(path)], capture_output=True,
                                  text=True, timeout=VALIDATE_TIMEOUT)
        except subprocess.TimeoutExpired as e:
            raise MapError(f"the validator did not finish within {VALIDATE_TIMEOUT}s") from e
    if proc.returncode == 0:
        return []
    return [line for line in (proc.stdout + proc.stderr).splitlines() if line.strip()] or ["validation failed"]


def save(key: str, token: str, draft: Path) -> None:
    """Accept a drawn map: check it, store it as the current map and mark the drawing saved. Raises MapError."""
    ws = workstream.require(db.connect(), key)
    with locked(ws):
        drawing = read_drawing(ws)
        if drawing is None or drawing.token != token:
            raise MapError("this drawing is not the one in progress (the token does not match)")
        try:
            doc = json.loads(Path(draft).read_text())
        except (OSError, ValueError) as e:
            raise MapError(f"the draft is not valid JSON: {e}") from e
        if not isinstance(doc, dict):
            raise MapError("the draft must be a JSON object")
        marks = [m for m in drawing.snapshot.values() if not m.error]
        first = marks[0] if marks else RepoMark(NO_SHA, NO_SHA, "")
        doc["provenance"] = {"repo": {"owner": "wm", "name": ws.key}, "base": {"sha": first.fork},
                             "head": {"sha": first.head}}
        problems = _validate(doc)
        prefixes = tuple(f"{repo}/" for repo in drawing.snapshot)
        lanes = doc.get("lanes", [])
        if not isinstance(lanes, list):
            raise MapError("lanes must be a list")
        for lane in lanes:
            lane_id = lane.get("id") if isinstance(lane, dict) else None
            if not isinstance(lane_id, str) or not lane_id.startswith(prefixes):
                problems.append(f"lane {lane_id!r} must start with the name of a repo in this map, e.g. "
                                f"{prefixes[0] if prefixes else 'repo/'}")
        if problems:
            raise MapError("\n".join(problems))
        _write_json(map_dir(ws) / "map.json", {"doc": doc, "drawn_at": time.time(),
                                                "snapshot": {r: asdict(m) for r, m in drawing.snapshot.items()},
                                                "token": token})
        drawing.saved = True
        write_drawing(ws, drawing)
