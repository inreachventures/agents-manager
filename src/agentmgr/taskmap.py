"""The task map: a PR Lens graph of what a workstream changes across its repos, and how to tell it has gone stale.

Map files live under `<workstream folder>/.wm/map/`, never inside a repo: `map.json` is the saved map, `drawing.json`
the drawing in progress, `lock` serialises changes to the drawing state, `brief.md` tells the drawing process what to
do, `draft.json` is what it writes, `last-run.log` its output, `failure.json` why the last drawing failed and
`attempted.json` the heads the last drawing started from."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.resources
import json
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from . import db, diffs, gitops, tmux, workstream
from .config import DRAWING_ENV, get_config

PRLENS = importlib.resources.files("agentmgr") / "data" / "prlens"
NO_SHA = "0" * 40
VALIDATE_TIMEOUT = 30
DRAWING_TIMEOUT = 15 * 60
PROMPT = "Draw this workstream's task map: follow .wm/map/brief.md"
NO_NODE = "The task map needs Node.js"
LOG_TAIL = 20
# Set by the Claude Code session whose hook starts a drawing; a drawing must not look like part of that session.
SESSION_ENV = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
               "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_MESSAGING_SOCKET",
               "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_SSE_PORT")


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
    pgid: int | None  # the drawing's process group: tells it apart from an unrelated process given a reused pid
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


def live_digest(link: db.RepoLink) -> str:
    return diff_digest(diffs.repo_diff(Path(link.worktree_path), link.base))


def repo_mark(link: db.RepoLink) -> RepoMark:
    return repo_mark_with(link, live_digest)


def repo_mark_with(link: db.RepoLink, digest_of: Callable[[db.RepoLink], str]) -> RepoMark:
    """Where `link`'s repo stands now; `digest_of` supplies its diff digest (a caller may have a recent one)."""
    wt = Path(link.worktree_path)
    try:
        if not wt.is_dir():
            raise gitops.GitError(f"worktree {wt} is missing")
        head = gitops.git(wt, "rev-parse", "HEAD")
        fork = gitops.git(wt, "merge-base", link.base, "HEAD") if gitops.ref_exists(wt, link.base) else head
        return RepoMark(head=head, fork=fork, digest=digest_of(link))
    except (gitops.GitError, OSError) as e:
        return RepoMark(head="", fork="", digest="", error=str(e))


def snapshot(conn, ws: db.Workstream) -> dict[str, RepoMark]:
    return {link.repo: repo_mark(link) for link in db.list_repos(conn, ws.key)}


def heads(conn, ws: db.Workstream) -> dict[str, str]:
    """Each repo's current HEAD, cheaply (no diff digest); repos whose worktree can't be read are left out."""
    out = {}
    for link in db.list_repos(conn, ws.key):
        try:
            out[link.repo] = gitops.git(Path(link.worktree_path), "rev-parse", "HEAD")
        except (gitops.GitError, OSError):
            continue
    return out


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


def _items(parent: object, key: str) -> list:
    """`parent[key]` when it is a list of objects (the validator reports any other shape), else []."""
    value = parent.get(key) if isinstance(parent, dict) else None
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _convention_problems(doc: dict, repos: list[str]) -> list[str]:
    """Where `doc` breaks wm's conventions: every lane id and every card's file path starts with a repo's folder (a
    repo-relative path, as in PR Lens's own examples, leaves the card unable to open its diff)."""
    prefixes = tuple(f"{repo}/" for repo in repos)
    example = prefixes[0] if prefixes else "repo/"
    problems = []
    if not isinstance(doc.get("lanes", []), list):
        problems.append("lanes must be a list")
    for lane in _items(doc, "lanes"):
        lane_id = lane.get("id")
        if not isinstance(lane_id, str) or not lane_id.startswith(prefixes):
            problems.append(f"lane {lane_id!r} must start with the name of a repo in this map, e.g. {example}")
    cards = [(f"nodes[{i}]", node) for i, node in enumerate(_items(doc, "nodes"))]
    cards += [(f"dataModel.tables[{i}]", table) for i, table in enumerate(_items(doc.get("dataModel"), "tables"))]
    for where, card in cards:
        for j, file in enumerate(_items(card, "files")):
            path = file.get("path")
            if isinstance(path, str) and not path.startswith(prefixes):
                problems.append(f"{where}.files[{j}].path {path!r} must start with the folder of a repo in this map, "
                                f"e.g. {example}{path}")
    return problems


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
        problems = _validate(doc) + _convention_problems(doc, list(drawing.snapshot))
        if problems:
            raise MapError("\n".join(problems))
        _write_json(map_dir(ws) / "map.json", {"doc": doc, "drawn_at": time.time(),
                                                "snapshot": {r: asdict(m) for r, m in drawing.snapshot.items()},
                                                "token": token})
        drawing.saved = True
        write_drawing(ws, drawing)
        (map_dir(ws) / "failure.json").unlink(missing_ok=True)


# ---------------------------------------------------------------------------- the drawing process


def draft_path(ws: db.Workstream) -> Path:
    return map_dir(ws) / "draft.json"


def last_failure(ws: db.Workstream) -> dict | None:
    """Why the last drawing failed, as {"at", "reason", "log"}; None once a drawing has saved. `log`: the drawing ran
    and its `last-run.log` is there to open (a drawing that never started has none of its own)."""
    try:
        raw = json.loads((map_dir(ws) / "failure.json").read_text())
        return {"at": float(raw["at"]), "reason": str(raw["reason"]),
                "log": raw.get("ran") is True and (map_dir(ws) / "last-run.log").is_file()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def attempted_heads(ws: db.Workstream) -> dict[str, str] | None:
    """Each repo's HEAD when the last drawing started (whether or not it saved); None if none is recorded."""
    try:
        heads = json.loads((map_dir(ws) / "attempted.json").read_text())["heads"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return {str(repo): str(head) for repo, head in heads.items()} if isinstance(heads, dict) else None


def _record_failure(ws: db.Workstream, reason: str, ran: bool) -> None:
    """`ran`: the drawing process ran (so its log is this failure's), rather than never starting."""
    _write_json(map_dir(ws) / "failure.json", {"at": time.time(), "reason": reason, "ran": ran})


def _log_tail(ws: db.Workstream) -> str:
    try:
        lines = (map_dir(ws) / "last-run.log").read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-LOG_TAIL:]).strip()


def _alive(pid: int, pgid: int) -> bool:
    """The drawing still runs: its pid exists and is still in the drawing's process group (a pid reused by an
    unrelated process is not)."""
    try:
        return os.getpgid(pid) == pgid
    except (ProcessLookupError, PermissionError):
        return False


def _kill(pid: int, pgid: int) -> None:
    """SIGTERM the drawing's process group, claude and whatever it runs; only while it is still the drawing's."""
    try:
        if _alive(pid, pgid) and pgid != os.getpgid(0):  # never our own group
            os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def _running(drawing: Drawing) -> bool:
    return drawing.pid is not None and drawing.pgid is not None and _alive(drawing.pid, drawing.pgid)


def _spawn(argv: list[str], cwd: Path, env: dict, log: Path) -> tuple[int, int]:
    """Start `argv` detached, output to `log`; returns its pid and process group. `sh` backgrounds it and exits at
    once, so the caller can exit too and nothing is left a zombie: the process is reparented to launchd. It stays in
    the process group of that `sh` (a new session, so the group id is the sh's pid), and `_kill` reaches everything
    it starts."""
    proc = subprocess.run(["/bin/sh", "-c", 'exec "$@" >"$LOG" 2>&1 </dev/null &\necho $$ $!', "sh", *argv],
                          env={**env, "LOG": str(log)}, cwd=cwd, start_new_session=True, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True)
    pgid, pid = map(int, proc.stdout.split())
    return pid, pgid


def _commands(ws: db.Workstream, links: list[db.RepoLink], drawing: Drawing) -> dict:
    """The commands the drawing process may run, exactly as the brief spells them and the allow rules match them.
    `diff` is each available repo's diff from its fork point: with a commit as its first argument, git diff never
    takes two paths as files to compare (its implicit `--no-index`, which reads files outside the repo)."""
    draft = shlex.quote(str(draft_path(ws)))
    validate = f"node {shlex.quote(str(PRLENS / 'prlens-validate.mjs'))}"
    save = f"{shlex.quote(tmux.wm_executable())} map save"
    git = {link.repo: f"git -C {shlex.quote(link.worktree_path)}" for link in links}
    marks = {repo: drawing.snapshot.get(repo) for repo in git}
    return {"git": git,
            "diff": {repo: f"{git[repo]} diff --find-renames {mark.fork}"
                     for repo, mark in marks.items() if mark is not None and not mark.error},
            "validate": validate, "save": save, "validate_draft": f"{validate} {draft}",
            "save_draft": f"{save} {ws.key} --token {drawing.token} {draft}"}


def allowed_tools(ws: db.Workstream, links: list[db.RepoLink], drawing: Drawing) -> list[str]:
    """What the drawing process may do beyond reading its working directories (the workstream folder and the
    vendored PR Lens folder, which need no rule); anything else is denied, never prompted. `//` starts an absolute
    path in a permission rule, and an `Edit` rule also covers the Write tool (a `Write(path)` rule is accepted but
    never matched)."""
    cmds = _commands(ws, links, drawing)
    rules = [f"Bash({diff} *)" for diff in cmds["diff"].values()]
    for git in cmds["git"].values():
        rules += [f"Bash({git} {sub} *)" for sub in ("log", "show", "status")]
    rules += [f"Edit(/{draft_path(ws)})", f"Bash({cmds['validate']} *)", f"Bash({cmds['save']} *)"]
    return rules


# `--output=<file>` makes git diff/log/show write a file anywhere; `--no-index` makes git diff read files anywhere
DISALLOWED_TOOLS = ["WebFetch", "WebSearch", "Bash(git * --output*)", "Bash(git * --no-index*)"]
TOOLS = "Bash,Read,Edit,Write,Glob,Grep"
# Passed with --settings, the only settings file a --restricted session reads besides managed settings.
DRAWING_SETTINGS = {"disableAllHooks": True, "permissions": {"blockReadsOutsideWorkingDirectories": True}}


def drawing_argv(claude: str, ws: db.Workstream, links: list[db.RepoLink], drawing: Drawing) -> list[str]:
    """`claude -p` confined to the brief. `--restricted` loads only managed settings and `--settings`, so no allow
    rule, hook, plugin or default mode from the user's, the workstream's or a repo's settings reaches it; it keeps
    the tools `--tools` names and confines the file tools to the working directories. `--safe-mode` loads no CLAUDE.md
    (whose rules are the workstream session's, not this brief's), skill, agent or auto memory. `--strict-mcp-config`
    loads no MCP server. `dontAsk` denies whatever would prompt. No transcript is saved, so the workstream's session
    never sees one beside its own."""
    return [claude, "-p", PROMPT, "--restricted", "--safe-mode", "--tools", TOOLS, "--strict-mcp-config",
            "--settings", json.dumps(DRAWING_SETTINGS), "--add-dir", str(PRLENS),
            "--permission-mode", "dontAsk", "--no-session-persistence",
            "--allowedTools", *allowed_tools(ws, links, drawing), "--disallowedTools", *DISALLOWED_TOOLS]


def brief_text(ws: db.Workstream, links: list[db.RepoLink], drawing: Drawing, existing: dict | None) -> str:
    versions = json.loads((PRLENS / "versions.json").read_text())
    cmds = _commands(ws, links, drawing)
    marks = [m for m in drawing.snapshot.values() if not m.error]
    first = marks[0] if marks else RepoMark(NO_SHA, NO_SHA, "")
    skeleton = {
        "schemaVersion": versions["schemaVersion"], "kind": "graph", "title": ws.label,
        "summary": "One paragraph: what does this workstream change?", "lenses": ["architecture", "data-flow"],
        "provenance": {"repo": {"owner": "wm", "name": ws.key}, "base": {"sha": first.fork},
                       "head": {"sha": first.head}},
        "lanes": [], "nodes": [], "edges": [], "flows": [],
    }
    repos = []
    for link in links:
        mark = drawing.snapshot.get(link.repo)
        git = cmds["git"][link.repo]
        if mark is None or mark.error:
            repos.append(f"- `{link.repo}/`: branch `{link.branch}`, base `{link.base}`. Unavailable right now"
                         f"{f' ({mark.error})' if mark and mark.error else ''}: leave it out of the map.")
            continue
        repos.append(f"- `{link.repo}/`: branch `{link.branch}`, base `{link.base}`, forked at `{mark.fork}`.\n"
                     f"  Its changes, committed and not: `{cmds['diff'][link.repo]}`. Only that diff command is "
                     f"allowed, with options or ` -- <path>` added after it. Also allowed: `{git} log ...`, "
                     f"`{git} show ...`, `{git} status`.")
    if existing:
        redraw = (f"A map was drawn before: `{map_dir(ws) / 'map.json'}` (the document is its `doc` field). Redraw "
                  "from it: keep the id of every lane, node, edge and flow that still exists, so its card stays in "
                  "place, and change only what the code has changed since.")
    else:
        redraw = "There is no map yet: draw it from scratch."
    return f"""# Draw the task map of workstream {ws.key} ({ws.label})

The task map is one PR Lens graph document showing what this workstream changes across all its repos: their
components, how they connect (across repos too), and how data flows through them. wm shows it beside the diff.
You run unattended: nobody will answer a question or approve a permission. Do only what this brief says.

## The repos

The workstream folder is `{ws.path}`; each repo is a subfolder of it.

{chr(10).join(repos) if repos else "- (no repos are attached)"}

Read any file in the workstream folder with Read, Glob and Grep. Nothing outside it is readable, apart from the
PR Lens folder below.

## The document format

Follow `{PRLENS / 'manual.md'}` for what a good map shows, and `{PRLENS / 'references'}/` for the document's
shape (`graph-document.md`, and `example.graph.json`, a complete valid document). Use them for the format only:
skip their steps that write `.pr-lens/`, run `npx`, render, comment or push a canvas. You cannot run those; the
commands below replace them.

## wm's conventions

- Every lane id starts with the folder name of its repo and a slash, e.g. `{links[0].repo if links else "repo"}/ui`.
  A repo may have several lanes; each lane belongs to exactly one repo.
- Every file path (`files[].path` on nodes and data-model tables) is relative to the workstream folder, so it
  starts with its repo's folder: `{links[0].repo if links else "repo"}/src/app.ts`, never `src/app.ts`. The PR Lens
  examples use repo-relative paths; here those would leave a card unable to open its diff, and save refuses them.
- Edges and flow steps may cross repos (a web client calling an API route). Those are the point of a task map.
- `provenance` is filled in by wm when you save; leave it as the skeleton has it.

## The draft

{redraw}

Write the document to `{draft_path(ws)}` (only this file), starting from this skeleton:

```json
{json.dumps(skeleton, indent=2)}
```

Check it with:

    {cmds['validate_draft']}

Fix every problem it reports and run it again until it prints `ok`. Then save it with:

    {cmds['save_draft']}

If save reports problems, fix them in the draft and save again. When save prints `saved`, you are done: stop.
"""


def _reap(conn, ws: db.Workstream) -> str | None:
    """Clear a drawing that has ended (or ran too long); rerun it once if it was requested again and a repo moved.
    Returns what that rerun's `start_drawing` returned, None if there was none."""
    rerun = False
    if not map_dir(ws).is_dir():  # no drawing ever started (or the workstream folder is gone)
        return None
    with locked(ws):
        drawing = read_drawing(ws)
        if drawing is None:
            return None
        alive = _running(drawing)
        timed_out = time.time() - drawing.started_at > DRAWING_TIMEOUT
        if alive and not timed_out:
            return None
        if alive:
            _kill(drawing.pid, drawing.pgid)
        write_drawing(ws, None)
        if not drawing.saved:
            reason = (f"timed out after {DRAWING_TIMEOUT // 60} minutes" if alive
                      else _log_tail(ws) or "the drawing stopped without saving a map")
            _record_failure(ws, reason, ran=True)
        if drawing.requested_again:
            rerun = bool(freshness(ws.key, drawing.snapshot, snapshot(conn, ws), db.list_repos(conn, ws.key)))
    return start_drawing(conn, ws) if rerun else None


def reap(conn, ws: db.Workstream) -> None:
    _reap(conn, ws)


def start_drawing(conn, ws: db.Workstream) -> str:
    """Start Claude drawing the map in the background: "started", "queued" (one is running; it reruns once it ends,
    if a repo moved meanwhile), "no-node", "no-claude" or "failed" (recorded as the last failure), or "unavailable"
    (the workstream is archived or its folder is gone: a hook can fire while it is being archived)."""
    current = db.get_workstream(conn, ws.key)
    if current is None or current.status == db.ARCHIVED or not ws.path.is_dir():
        return "unavailable"
    if (rerun := _reap(conn, ws)) is not None:
        return rerun
    with locked(ws):
        drawing = read_drawing(ws)
        if drawing is not None:
            drawing.requested_again = True
            write_drawing(ws, drawing)
            return "queued"
        if shutil.which("node") is None:
            _record_failure(ws, NO_NODE, ran=False)
            return "no-node"
        claude = shutil.which(get_config().claude_cmd)
        if claude is None:
            _record_failure(ws, workstream.claude_missing() or "Claude Code isn't installed", ran=False)
            return "no-claude"
        links = db.list_repos(conn, ws.key)
        drawing = Drawing(token=secrets.token_hex(16), pid=None, pgid=None, started_at=time.time(),
                          snapshot=snapshot(conn, ws))
        (map_dir(ws) / "brief.md").write_text(brief_text(ws, links, drawing, load(ws)))
        draft_path(ws).unlink(missing_ok=True)
        _write_json(map_dir(ws) / "attempted.json",
                    {"heads": {repo: mark.head for repo, mark in drawing.snapshot.items() if not mark.error}})
        argv = drawing_argv(claude, ws, links, drawing)
        env = {k: v for k, v in os.environ.items() if k not in SESSION_ENV} | {DRAWING_ENV: "1"}
        try:
            drawing.pid, drawing.pgid = _spawn(argv, ws.path, env, map_dir(ws) / "last-run.log")
        except (OSError, ValueError) as e:
            _record_failure(ws, f"the drawing could not start: {e}", ran=False)
            return "failed"
        write_drawing(ws, drawing)
    return "started"


def cancel(ws: db.Workstream) -> None:
    """Stop a drawing in progress and forget it (the workstream is being archived)."""
    if not map_dir(ws).is_dir():
        return
    with locked(ws):
        drawing = read_drawing(ws)
        if drawing is None:
            return
        if _running(drawing):
            _kill(drawing.pid, drawing.pgid)
        write_drawing(ws, None)
