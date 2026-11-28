"""`wm app-server`: the backend of the native macOS app, speaking JSON lines on stdin/stdout.

The app sends `{"id": 1, "method": "archive", "params": {"key": "WS-7"}}` and gets back `{"id": 1, "result": ...}` or
`{"id": 1, "error": "..."}`. Unprompted, the server sends `{"event": "state", "data": ...}` whenever the dashboard
state changes (checked every 2s, like the TUI; every 10s while the app is in the background) and
`{"event": "notice", "data": {"level": ..., "message": ...}}`.

Plugins (see plugins.py) add requests (`plugins`, `plugin_request`, `set_plugin`; `send` types into a session for
their views), badges on the state's rows and two events: `plugin_changed` when a path a plugin watches changes, and
`plugins_changed` when the plugins folder changes, after which the app restarts the server (Python can't unload a
plugin's modules).

The app is only a view: every change goes through the workstream service, exactly as from the CLI and the TUI, so
worktrees, branches and guardrails stay wm's job. Requests run in threads, so a slow archive doesn't hold up the rest.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import queue
import shutil
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any, TextIO

from . import db, diffs, fetcher, gh, gitops, plugins, sessioncolor, tmux, view, workstream
from .config import Config, get_config, save_paths
from .naming import NamingError
from .plugin import Badge, PluginError
from .repos import RepoMatchError, discover

STATE_SECONDS = 2
BACKGROUND_STATE_SECONDS = 10  # while the app is behind other apps, when only its dock badge shows
BACKGROUND_GIT_MAX_AGE = 60  # view.GIT_MAX_AGE while the app is in the background
PR_REFRESH_SECONDS = 60
COLOR_WAIT_SECONDS = 10  # after typing /color, how long to show the picked colour while Claude's transcript lacks it
FETCH_SECONDS = 30  # each repo is actually fetched at most every fetcher.DASHBOARD_MAX_AGE
BADGE_SECONDS = 0.05  # a plugin's badge function runs on every state push; one taking longer is logged
PLUGIN_REQUEST_SECONDS = 2.0  # a plugin request taking longer is logged: a view is waiting on it
PLUGIN_WORKERS = 4  # threads for plugin requests, apart from core's
EXPECTED = (workstream.WorkstreamError, NamingError, RepoMatchError, gitops.GitError, tmux.TmuxError, PluginError)
# Requests that change nothing the dashboard shows, so no state push after them: the app asks for a diff every 3s,
# and pushing the state after each one kept the state loop (git in every repo) running without a pause. Plugin
# requests are among them: a view polling its plugin mustn't keep the state loop busy either.
QUIET = frozenset({"state", "repos", "rebase_plan", "archive_report", "setup", "scan", "diff", "focus", "plugins",
                   "plugin_request"})

# view.git_state's rich styles, as colour names the app maps to its palette
TONES = {"bold red": "red", "yellow": "yellow", view.BLUE: "blue", "bold green": "green", "grey50": "grey",
         "cyan": "cyan", "dark_orange": "orange", view.PURPLE: "purple"}


def _repo(rr: view.RepoRow) -> dict:
    link = rr.link
    return {
        "repo": link.repo, "branch": link.branch, "base": link.base, "worktree": link.worktree_path,
        "state": rr.state, "tone": TONES.get(rr.style, ""), "behind": rr.behind,
        "behind_warn": rr.behind >= fetcher.BEHIND_WARN, "pr": rr.pr, "pr_url": rr.pr_url, "merged": rr.merged,
        "fetch_error": rr.fetch_error,
        # each PR the PR column lists, to link to; pr_url is the one that matters now
        "prs": [{"number": p.number, "url": p.url, "label": gh.describe(p)} for p in rr.prs],
    }


WAITING = (db.NEEDS_YOU, db.YOUR_TURN)


def _row(r: view.Row, unseen: bool, color: str | None, badges: list[dict]) -> dict:
    ws = r.ws
    example = workstream.is_example(ws) and r.status != db.ARCHIVED
    return {
        "key": ws.key, "ticket": ws.ticket, "name": ws.name, "label": ws.label, "status": r.status,
        "unseen": unseen,  # waiting on you, and nobody has looked at the session since it started waiting
        "color": color,  # Claude's /color for the session (see sessioncolor), None for its default
        "status_text": "example · ready to archive" if example else r.status_text,
        "since": ws.archived_at or ws.status_at, "folder": ws.folder,
        "intake": r.intake, "drift": ws.drift, "all_merged": r.all_merged, "base_ci": r.base_ci,
        # everything merged and CI passed on base, or no CI there: the TUI's "→ x archive"
        "ready_to_archive": example or (r.all_merged and r.base_ci in ("pass", None)),
        "repos": [_repo(rr) for rr in r.repos],
        "badges": badges,  # from plugins: [{plugin, text, tone, help}]
    }


def state(colors: dict[str, str], git_max_age: float = 0) -> dict:
    """Everything the dashboard shows, in the TUI's order: needs you, working, your turn, intake, stopped.
    `colors`: session colours chosen in the app that Claude doesn't show yet (Server.colors). `git_max_age`: see
    view.load. Marks the waiting sessions someone is looking at as seen (_unseen)."""
    tmux.prune_views()
    rows = view.load(include_archived=True, git_max_age=git_max_age)
    active = [r for r in rows if r.status != db.ARCHIVED]
    oks = [rr.fetched_ok_at for r in active for rr in r.repos]
    clients = tmux.client_sessions()
    unseen = _unseen(active, clients)
    badges = _badges(active)

    def color(ws: db.Workstream) -> str | None:
        chosen = colors.get(ws.key)
        return (chosen if chosen in sessioncolor.COLORS else None) if chosen else sessioncolor.current(ws)

    return {
        "workstreams": [_row(r, r.ws.key in unseen, color(r.ws), badges.get(r.ws.key, [])) for r in active],
        "archived": [_row(r, False, None, []) for r in rows if r.status == db.ARCHIVED],
        "origin": {"repos": len(oks), "checked_at": None if None in oks or not oks else min(oks)},
        # client pid -> the session it shows, so the app notices when tmux moved its terminal (C-b s, say)
        "clients": {str(pid): session for pid, session in clients.items()},
    }


def _badges(rows: list[view.Row]) -> dict[str, list[dict]]:
    """Each workstream's plugin badges, by key. A plugin's trouble costs only its own badge, never the state push."""
    try:
        lps = [lp for lp in plugins.loaded() if lp.api.registered.badge]
    except Exception:  # noqa: BLE001 — e.g. config.toml unreadable: no badges, the dashboard still goes out
        traceback.print_exc()
        return {}
    out: dict[str, list[dict]] = {}
    for r in rows if lps else []:
        info = plugins.workstream_info(r.ws, [rr.link for rr in r.repos])
        marks = out.setdefault(r.ws.key, [])
        for lp in lps:
            name = lp.manifest.name
            badge = plugins.call(lp, "badge", lp.api.registered.badge, info, budget=BADGE_SECONDS, convert=_badge)
            if badge is not None:
                # str(): whatever the plugin put there, the state must stay JSON
                marks.append({"plugin": name, "text": str(badge.text), "tone": str(badge.tone),
                              "help": str(badge.help)})
    return out


def _badge(result: Any) -> Badge | None:
    if result is not None and not isinstance(result, Badge):
        raise plugins.WrongResult(f"returned {type(result).__name__}, not a Badge")
    return result


def _watch_stamp(paths: Any) -> tuple | None:
    return None if paths is None else _stamp(paths)


def _stamp(paths: Any) -> tuple:
    """What changes when one of `paths` (from a plugin's api.watch) is written, created or removed."""
    if isinstance(paths, str | bytes | os.PathLike):
        raise TypeError(f"watch returned {type(paths).__name__}, not a list of paths")
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append((os.fspath(p), st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((os.fspath(p), None, None))
    return tuple(out)


def _plugin(name: str, path: Path, m: plugins.Manifest | None, status: str, reason: str | None) -> dict:
    """A plugin as the app lists it. Only a plugin that is on offers its views (`page`: an absolute path, kept inside
    the plugin's folder by plugins._view)."""
    views = [{"id": v.id, "title": v.title, "place": v.place, "page": str((m.path / v.page).resolve()),
              "shortcut": v.shortcut} for v in m.views] if m and status == "on" else []
    return {"name": name, "title": m.title if m else name, "version": m.version if m else "", "status": status,
            "reason": reason, "path": str(path.resolve()), "views": views}  # resolved, as `page` is


def plugin_list() -> list[dict]:
    """Every plugin in the plugins folder, in name order: on, off, or skipped with the reason."""
    on = {lp.manifest.name for lp in plugins.loaded()}  # first, so that skipped() has the plugins that fail to load
    failed = {s.name: s.reason for s in plugins.skipped()}
    manifests, skips = plugins.discover()
    out = [_plugin(m.name, m.path, m, "on", None) if m.name in on
           else _plugin(m.name, m.path, m, "skipped", failed.get(m.name, "not loaded yet")) for m in manifests]
    for s in skips:
        if s.reason == "turned off":
            out.append(_plugin(s.name, s.path, plugins.read_manifest(s.path), "off", None))
        else:
            out.append(_plugin(s.name, s.path, None, "skipped", s.reason))
    return sorted(out, key=lambda p: p["name"])


def _unseen(rows: list[view.Row], clients: dict[int, str]) -> set[str]:
    """Keys of the workstreams waiting on you that nobody has looked at since they started waiting. Writes to the
    database: marks those someone is looking at right now (a terminal shows the session, or the app does while in
    front; `clients`: tmux.client_sessions) as seen."""
    conn = db.connect()
    seen = db.seen_times(conn)
    waiting = [r.ws for r in rows if r.status in WAITING and seen.get(r.ws.key, 0) < r.ws.status_at]
    checked_at = time.time()
    looking = tmux.watched_sessions(clients.values()) if waiting else set()
    for ws in waiting:
        if ws.key in looking:
            db.mark_seen(conn, ws.key, checked_at)
    return {ws.key for ws in waiting if ws.key not in looking}


def _paths(values: list[str]) -> list[Path]:
    return [Path(os.path.expanduser(v)).resolve() for v in values]


def _scan(cfg: Config) -> dict:
    """The repos `cfg` finds, and what is wrong with its folders."""
    problems = [f"{p} is not a folder" for p in cfg.code_roots if not p.is_dir()]
    problems += [f"{p} is not a git repository (no .git folder)" for p in cfg.repos if not (p / ".git").is_dir()]
    found = discover(cfg)
    names = {r.name: r.path for r in found}
    problems += [f"{p}: a repo named {p.name} is already in {names[p.name].parent}" for p in cfg.repos
                 if (p / ".git").is_dir() and names.get(p.name) != p]
    return {"found": [r.name for r in found], "problems": problems}


def setup() -> dict:
    """What the welcome guide shows: where repos come from, what is found there, and the tools wm needs."""
    cfg = get_config()
    return {
        "code_roots": [str(p) for p in cfg.code_roots], "repos": [str(p) for p in cfg.repos],
        "workstreams_dir": str(cfg.workstreams_dir), "config_file": str(cfg.config_file),
        "from_env": "AGENTMGR_CODE_ROOTS" in os.environ,  # the file's code_roots don't apply then
        # claude: the command sessions run, as New and Resume check it
        "tools": {"claude": shutil.which(cfg.claude_cmd), **{t: shutil.which(t) for t in ("tmux", "git", "gh")}},
        **_scan(cfg),
    }


class Workers:
    """A few daemon threads taking calls from a queue, started at the first call. For plugin requests: Python joins a
    ThreadPoolExecutor's threads when it exits, so a plugin handler that never returns would keep an old app-server
    alive after the app quit; daemon threads are left behind instead."""

    def __init__(self, count: int, name: str):
        self.count, self.name = count, name
        self.calls: queue.SimpleQueue = queue.SimpleQueue()
        self.started = False
        self.lock = threading.Lock()

    def submit(self, fn, *args) -> None:
        with self.lock:
            if not self.started:
                self.started = True
                for i in range(self.count):
                    threading.Thread(target=self._work, daemon=True, name=f"{self.name}-{i}").start()
        self.calls.put((fn, args))

    def _work(self) -> None:
        while True:
            fn, args = self.calls.get()
            try:
                fn(*args)
            except Exception:  # noqa: BLE001 — keep the thread for the next call
                traceback.print_exc()


def _method(line: str) -> str | None:
    try:
        return str(json.loads(line).get("method"))
    except (ValueError, AttributeError):
        return None


class Server:
    def __init__(self, out: TextIO):
        self.out = out
        self.lock = threading.Lock()  # one message per line, whichever thread sends it
        self.wake = threading.Event()  # push the state now instead of at the next tick
        self.done = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="wm-request")
        self.plugin_workers = Workers(PLUGIN_WORKERS, "wm-plugin-request")  # a slow plugin never holds up core
        self.last_state: dict | None = None
        self.app_active = True  # the app is the frontmost app (rpc_focus)
        self.fresh = False  # the next state push reads every repo's git state again (rpc_refresh)
        # per workstream: the session colour picked in the app (a sessioncolor.COLORS name or "default") and when wm
        # typed it into the session (None: not yet, Claude is busy). Shown until Claude's transcript has it.
        self.colors: dict[str, tuple[str, float | None]] = {}
        self.colors_lock = threading.Lock()
        self.typing = threading.Lock()  # one apply_colors at a time, so a colour is typed once
        self.locks: dict[str, threading.Lock] = {}  # per workstream: one change at a time (an add-repo during an
        self.locks_lock = threading.Lock()  # archive would leave a worktree behind)
        # Plugins, as check_plugins and check_watched see them. Only push_state reads and writes these, and only the
        # state loop's thread runs push_state, so they need no lock.
        self.plugins_seen: str | None = None  # plugins.fingerprint() as this server started with it
        self.noticed: set[str] = set()  # plugins whose skip was announced, once per server run
        self.noticed_errors: set[tuple[str, str]] = set()  # (plugin, what) handler errors announced, likewise
        self.watched: dict[tuple[str, str], tuple] = {}  # (plugin, workstream key) -> _stamp of its watched paths

    def send(self, msg: dict) -> None:
        # allow_nan=False: NaN or Infinity isn't JSON and the app could not read the line; a request gets an error
        line = json.dumps(msg, separators=(",", ":"), allow_nan=False) + "\n"
        with self.lock:
            try:
                self.out.write(line)
                self.out.flush()
            except (BrokenPipeError, ValueError):  # the app is gone
                self.done.set()

    def locked(self, key: str) -> threading.Lock:
        with self.locks_lock:
            return self.locks.setdefault(key.upper(), threading.Lock())

    def notice(self, message: str, level: str = "info") -> None:
        self.send({"event": "notice", "data": {"level": level, "message": message}})

    # ------------------------------------------------------------------ loop

    def serve(self, stdin: TextIO) -> None:
        try:
            tmux.reload_conf()
        except tmux.TmuxError:
            traceback.print_exc()
        try:
            workstream.repoint_hooks()
        except Exception:  # noqa: BLE001 (regenerating runs plugin code: the server starts whatever it raises)
            traceback.print_exc()
        for fn, seconds in ((self.push_state, None), (self.refresh_prs, PR_REFRESH_SECONDS),
                            (self.fetch_bases, FETCH_SECONDS)):
            threading.Thread(target=self._every, args=(fn, seconds), daemon=True).start()
        self.start_jobs()
        while line := stdin.readline():
            if line.strip():
                (self.plugin_workers if _method(line) == "plugin_request" else self.pool).submit(self.call, line)
        self.done.set()
        self.pool.shutdown(wait=True)  # an archive or rebase in flight runs to the end; a plugin request is left
        with self.typing:  # nor quit halfway through typing a /color, which would leave it in the prompt
            pass
        plugins.flush()

    def _every(self, fn, seconds: float | None) -> None:
        while not self.done.is_set():
            try:
                fn()
            except Exception:  # noqa: BLE001 — keep the loop alive; the next tick tries again
                traceback.print_exc()
            if seconds is None:  # the state loop: every STATE_SECONDS (slower in the background), or when woken
                self.wake.wait(STATE_SECONDS if self.app_active else BACKGROUND_STATE_SECONDS)
                self.wake.clear()
            else:
                self.done.wait(seconds)

    def start_jobs(self) -> None:
        """Each plugin's `api.every` job on a thread of its own, so a slow or failing job holds up nothing else."""
        try:
            lps = plugins.loaded()
        except Exception:  # noqa: BLE001 — no plugin jobs, the rest of the server runs on
            traceback.print_exc()
            return
        for lp in lps:
            for seconds, fn in lp.api.registered.every:
                job = partial(plugins.call, lp, f"every {seconds:g}s", fn)
                threading.Thread(target=self._every, args=(job, seconds), daemon=True,
                                 name=f"wm-plugin-{lp.manifest.name}").start()

    def git_max_age(self) -> float:
        return view.GIT_MAX_AGE if self.app_active else BACKGROUND_GIT_MAX_AGE

    def push_state(self) -> None:
        fresh, self.fresh = self.fresh, False
        for step in (self.apply_colors, self.check_plugins):
            try:
                step()
            except Exception:  # noqa: BLE001 — the dashboard goes out regardless
                traceback.print_exc()
        data = state(self.chosen_colors(), git_max_age=0 if fresh else self.git_max_age())
        if data != self.last_state:
            self.last_state = data
            self.send({"event": "state", "data": data})
        try:
            self.check_watched()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        plugins.flush()  # the handler errors of this push (badges, watches), in one write

    def check_plugins(self) -> None:
        """When the plugins folder changed since the server started (a plugin added, removed, edited or turned on or
        off), rewrite each workstream's CLAUDE.md and settings and send `plugins_changed`: the app then restarts the
        server, which can't unload the old code. And announce each skipped plugin, and each (plugin, handler) with an
        error recorded by any wm process, once per server run."""
        now = plugins.fingerprint()
        if self.plugins_seen is None:
            self.plugins_seen = now
        elif now != self.plugins_seen:
            self.plugins_seen = now
            plugins.reset()  # so the files are written from the plugins as they are now
            conn = db.connect()
            for ws in db.list_workstreams(conn):
                try:
                    workstream.regenerate(conn, ws)
                except Exception:  # noqa: BLE001 — one workstream's trouble mustn't keep the others stale
                    traceback.print_exc()
            self.send({"event": "plugins_changed", "data": {}})
        plugins.loaded()  # tries to load each plugin, so that skipped() has those that fail to
        titles = {m.name: m.title for m in plugins.discover()[0]}
        for s in plugins.skipped():
            if s.name not in self.noticed:
                self.noticed.add(s.name)
                self.notice(f"{titles.get(s.name, s.name)} plugin skipped: {s.reason}", "warning")
        for e in plugins.persisted_errors():  # this process's and those hooks and commands recorded
            if (e.name, e.what) not in self.noticed_errors:
                self.noticed_errors.add((e.name, e.what))
                self.notice(f"{titles.get(e.name, e.name)} plugin: {e.what}: {e.reason}", "warning")

    def check_watched(self) -> None:
        """Send `plugin_changed` for each (plugin, workstream) whose watched paths changed since the last state push.
        The first look at them only takes note."""
        lps = [lp for lp in plugins.loaded() if lp.api.registered.watch]
        conn = db.connect()
        seen: dict[tuple[str, str], tuple] = {}
        for ws in db.list_workstreams(conn) if lps else []:
            info = plugins.workstream_info(ws, db.list_repos(conn, ws.key))
            for lp in lps:
                name, at = lp.manifest.name, (lp.manifest.name, ws.key)
                # None: the function raised, returned nothing or not a list of paths; then nothing is known to have
                # changed
                stamp = plugins.call(lp, "watch", lp.api.registered.watch, info, convert=_watch_stamp)
                if stamp is None:
                    stamp = self.watched.get(at)
                if stamp is None:
                    continue
                if at in self.watched and self.watched[at] != stamp:
                    self.send({"event": "plugin_changed", "data": {"plugin": name, "key": ws.key}})
                seen[at] = stamp
        self.watched = seen

    def chosen_colors(self) -> dict[str, str]:
        with self.colors_lock:
            return {key: color for key, (color, _) in self.colors.items()}

    def apply_colors(self) -> None:
        """Type each colour picked in the app into its session once Claude waits at an empty prompt, and forget it
        once Claude's transcript has it (or a while after typing it, if Claude ignored it)."""
        with self.typing:
            self._apply_colors()

    def _apply_colors(self) -> None:
        with self.colors_lock:
            todo = dict(self.colors)
        for key, entry in todo.items():
            try:
                self._apply_color(key, *entry)
            except Exception:  # noqa: BLE001 — one session's trouble mustn't hold up the others or the state push
                traceback.print_exc()

    def _apply_color(self, key: str, color: str, sent_at: float | None) -> None:
        conn = db.connect()
        ws = db.get_workstream(conn, key)
        if ws is None or ws.status == db.ARCHIVED:
            done = True
        elif sent_at is not None:
            now = sessioncolor.current(ws) or "default"
            done = now == color or time.time() - sent_at > COLOR_WAIT_SECONDS
        else:
            done = False
            if (ws.status in (db.YOUR_TURN, db.INTAKE) and workstream.session_alive(ws)
                    and sessioncolor.prompt_ready(tmux.capture(ws.key), sessioncolor.vim_keys())):
                done = not self._type_color(ws, color)
                if not done:
                    with self.colors_lock:
                        if self.colors.get(key) == (color, None):
                            self.colors[key] = (color, time.time())
        if done:
            with self.colors_lock:
                if self.colors.get(key) == (color, sent_at):
                    del self.colors[key]

    def _type_color(self, ws: db.Workstream, color: str) -> bool:
        """Type `/color <color>` and press Enter, unless something else reached the prompt in between (you started
        typing): then leave it there for you to see, unsent, and give up on the pick. False if given up."""
        command = f"/color {color}"
        tmux.type_text(ws.key, command)
        time.sleep(0.5)  # a later, separate Enter submits; sent with the text it can be taken as part of a paste
        if sessioncolor.prompt_text(tmux.capture(ws.key)) != command:
            self.notice(f"{ws.key}: you started typing as wm set the colour, so it didn't send `{command}`",
                        "warning")
            return False
        tmux.press_enter(ws.key)
        return True

    def call(self, line: str) -> None:
        try:
            req = json.loads(line)
            rid, method, params = req.get("id"), str(req.get("method", "")), req.get("params") or {}
        except (ValueError, AttributeError):
            return
        try:
            fn = getattr(self, f"rpc_{method}", None)
            if fn is None:
                raise LookupError(f"unknown method {method!r}")
            self.send({"id": rid, "result": fn(**params)})
        except EXPECTED as e:
            self.send({"id": rid, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.send({"id": rid, "error": f"{type(e).__name__}: {e}"})
        finally:
            if method not in QUIET:
                self.wake.set()  # show the effect of the request right away

    def refresh_prs(self) -> None:
        conn = db.connect()
        for ws in db.list_workstreams(conn):
            gh.refresh(conn, ws.key)
        self.wake.set()

    def fetch_bases(self, max_age: float = fetcher.DASHBOARD_MAX_AGE) -> list[str]:
        errors = fetcher.fetch_stale(max_age)
        failed = sorted(p for p, e in errors.items() if e)
        if failed:
            self.notice(f"could not fetch: {', '.join(failed)}", "warning")
        if errors:
            self.wake.set()
        return failed

    # ------------------------------------------------------------------ requests (rpc_<method>)

    def rpc_state(self) -> dict:
        return state(self.chosen_colors(), git_max_age=self.git_max_age())

    def rpc_set_color(self, key: str, color: str) -> dict:
        """Give the workstream's Claude session this /color (a sessioncolor.COLORS name or "default"). wm types it
        into the session now if Claude waits at an empty prompt, else when it next does (`applied`: False)."""
        if color not in (*sessioncolor.COLORS, "default"):
            raise workstream.WorkstreamError(f"unknown colour {color!r}")
        ws = workstream.require(db.connect(), key)
        if ws.status == db.ARCHIVED:
            raise workstream.WorkstreamError(f"{ws.key} is archived")
        with self.colors_lock:
            self.colors[ws.key] = (color, None)
        self.apply_colors()
        with self.colors_lock:
            entry = self.colors.get(ws.key)
        return {"applied": entry is None or entry[1] is not None}

    def rpc_repos(self) -> dict:
        return {"repos": [r.name for r in discover(get_config())]}

    def rpc_create_example(self) -> dict:
        ws = workstream.new_example()
        return {"key": ws.key if ws else None}

    def rpc_new(self, ticket: str | None = None) -> dict:
        ws = workstream.new(ticket=ticket or None)
        return {"key": ws.key}

    def rpc_add_repo(self, key: str, repo: str, branch: str | None = None) -> dict:
        with self.locked(key):
            res = workstream.add_repo(key, repo, branch=branch or None)
        return {"repo": res.link.repo, "branch": res.link.branch, "created_branch": res.created_branch,
                "notes": res.notes}

    def rpc_resume(self, key: str) -> dict:
        with self.locked(key):
            return {"key": workstream.resume(key).key}

    def rpc_rebase_plan(self, key: str) -> dict:
        ws, steps = workstream.rebase_plan(key)
        return {"label": ws.label, "steps": [
            {"repo": s.link.repo, "branch": s.link.branch, "base": s.link.base, "behind": s.behind,
             "skip": s.skip, "push": s.pushed_at is not None} for s in steps]}

    def rpc_rebase(self, key: str) -> dict:
        with self.locked(key):
            return {"log": workstream.rebase(key)}

    def rpc_archive_report(self, key: str) -> dict:
        ws, reports = workstream.cleanup_report(key)
        repos = []
        for r in reports:
            label, style = view.git_state(r.link, r.status, r.pr, after_pr=r.after_pr, in_base=r.in_base,
                                          others=r.others)
            repos.append({"repo": r.link.repo, "branch": r.link.branch, "state": label,
                          "tone": TONES.get(style, ""), "pr": r.describe_prs(), "blockers": r.blockers})
        return {"label": ws.label, "repos": repos, "blocked": any(r.blockers for r in reports)}

    def rpc_archive(self, key: str, force: bool = False) -> dict:
        with self.locked(key):
            return {"log": workstream.archive(key, force=force)}

    def rpc_unarchive(self, key: str) -> dict:
        with self.locked(key):
            return {"log": workstream.unarchive(key)}

    def rpc_setup(self) -> dict:
        return setup()

    def rpc_scan(self, code_roots: list[str], repos: list[str]) -> dict:
        """Preview a choice of folders without saving it."""
        return _scan(dataclasses.replace(get_config(), code_roots=tuple(_paths(code_roots)),
                                         repos=tuple(_paths(repos))))

    def rpc_save_setup(self, code_roots: list[str], repos: list[str]) -> dict:
        """Save the folders to config.toml and rewrite each workstream's CLAUDE.md (its list of available repos)
        and settings (the checkouts it may not edit)."""
        roots, singles = _paths(code_roots), _paths(repos)
        if problems := self.rpc_scan(code_roots, repos)["problems"]:
            raise workstream.WorkstreamError("; ".join(problems))
        save_paths(roots, singles)
        conn = db.connect()
        for ws in db.list_workstreams(conn):
            workstream.regenerate(conn, ws)
        return setup()

    def rpc_focus(self, active: bool) -> dict:
        """The app came to the front or went to the back: hooks notify only when nobody is looking (tmux.watched)."""
        tmux.set_app_active(active)
        self.app_active = active
        if active:
            self.wake.set()  # catch up now, not at the end of the slower background tick
        return {}

    def rpc_refresh(self) -> dict:
        self.fresh = True
        self.refresh_prs()
        return {"failed": self.fetch_bases(max_age=0)}

    def rpc_diff(self, key: str, since: str | None = None) -> dict:
        """Each repo's changes. `since`: the digest of the diff the app shows; if nothing changed, only that is
        returned, so the app doesn't decode the same diff every few seconds."""
        conn = db.connect()
        ws = workstream.require(conn, key)
        repos = []
        for link in db.list_repos(conn, ws.key):
            entry = {"repo": link.repo, "branch": link.branch, "base": link.base}
            try:
                if not Path(link.worktree_path).exists():
                    raise gitops.GitError("worktree missing")
                entry |= diffs.repo_diff(Path(link.worktree_path), link.base)
            except gitops.GitError as e:
                entry |= {"error": str(e), "files": [], "additions": 0, "deletions": 0}
            repos.append(entry)
        digest = hashlib.sha1(json.dumps(repos, sort_keys=True).encode()).hexdigest()[:16]
        if digest == since:
            return {"key": ws.key, "digest": digest, "unchanged": True, "repos": []}
        return {"key": ws.key, "digest": digest, "repos": repos}

    def rpc_attach(self, key: str) -> dict:
        """argv for the app's terminal: a tmux client on the workstream's app view."""
        ws = workstream.require(db.connect(), key)
        if not workstream.session_alive(ws):
            raise workstream.WorkstreamError(f"{ws.key} is stopped; resume it first")
        return {"argv": tmux.attach_command(tmux.ensure_view(ws.key))}

    def rpc_send(self, key: str, text: str) -> dict:
        """Type `text` into the workstream's Claude session and press Enter, as a plugin's view asks."""
        if not isinstance(text, str) or not text:
            raise PluginError("text must be non-empty text")
        ws = workstream.require(db.connect(), key)
        if not workstream.session_alive(ws):
            raise workstream.WorkstreamError(f"{ws.key} is stopped; resume it first")
        with self.typing:  # never into the middle of a /color wm is typing
            tmux.send_text(ws.key, text)
        return {}

    def rpc_plugins(self) -> dict:
        return {"plugins": plugin_list(), "dir": str(get_config().plugins_dir)}

    def rpc_set_plugin(self, name: str, on: bool) -> dict:
        """Turn a plugin on or off. The next state push sees the plugins folder changed (check_plugins)."""
        if not isinstance(on, bool):
            raise PluginError("on must be true or false")
        manifests, skips = plugins.discover()
        if name not in {m.name for m in manifests} | {s.name for s in skips}:
            raise PluginError(f"no plugin named '{name}'")
        plugins.set_enabled(name, on)
        return {}

    def rpc_plugin_request(self, plugin: str, method: str, params: dict) -> Any:
        """Call the handler a plugin registered with `api.request(method, fn)` as `fn(**params)`, for its web views.
        Its PluginError comes back as the message alone, anything else as "<Type>: <message>"."""
        if not isinstance(params, dict):
            raise PluginError("params must be an object")
        fn = next((lp.api.registered.requests.get(method) for lp in plugins.loaded() if lp.manifest.name == plugin),
                  None)
        if fn is None:
            raise PluginError(f"plugin {plugin} has no request {method!r}")
        start = time.monotonic()
        try:
            return fn(**params)
        except SystemExit as e:  # would otherwise leave the request unanswered
            traceback.print_exc()
            raise PluginError(f"SystemExit: {e}") from e
        finally:
            if (secs := time.monotonic() - start) > PLUGIN_REQUEST_SECONDS:
                plugins.log(f"plugin {plugin}: request {method} took {secs:.2f}s")

    def rpc_switch(self, key: str, pid: int) -> dict:
        """Point the app's running tmux client (process `pid`) at another workstream; False if there is none."""
        ws = workstream.require(db.connect(), key)
        client = tmux.client_of(pid)
        if client is None or not workstream.session_alive(ws):
            return {"switched": False}
        tmux.switch_client(tmux.ensure_view(ws.key), client)
        return {"switched": True}


def main() -> None:
    out = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())  # stray prints and child processes never reach the protocol
    plugins.batch_errors()  # badges and watches run for every workstream every few seconds: write once per push
    Server(out).serve(sys.stdin)
