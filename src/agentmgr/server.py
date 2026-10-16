"""`wm app-server`: the backend of the native macOS app, speaking JSON lines on stdin/stdout.

The app sends `{"id": 1, "method": "archive", "params": {"key": "WS-7"}}` and gets back `{"id": 1, "result": ...}` or
`{"id": 1, "error": "..."}`. Unprompted, the server sends `{"event": "state", "data": ...}` whenever the dashboard
state changes (checked every 2s, like the TUI) and `{"event": "notice", "data": {"level": ..., "message": ...}}`.

The app is only a view: every change goes through the workstream service, exactly as from the CLI and the TUI, so
worktrees, branches and guardrails stay wm's job. Requests run in threads, so a slow archive doesn't hold up the rest.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TextIO

from . import db, diffs, fetcher, gh, gitops, tmux, view, workstream
from .config import get_config
from .naming import NamingError
from .repos import RepoMatchError, discover

STATE_SECONDS = 2
PR_REFRESH_SECONDS = 60
FETCH_SECONDS = 30  # each repo is actually fetched at most every fetcher.DASHBOARD_MAX_AGE
EXPECTED = (workstream.WorkstreamError, NamingError, RepoMatchError, gitops.GitError, tmux.TmuxError)

# view.git_state's rich styles, as colour names the app maps to its palette
TONES = {"bold red": "red", "yellow": "yellow", view.BLUE: "blue", "bold green": "green", "grey50": "grey",
         "cyan": "cyan", "dark_orange": "orange", "bold medium_purple1": "purple"}


def _repo(rr: view.RepoRow) -> dict:
    link = rr.link
    return {
        "repo": link.repo, "branch": link.branch, "base": link.base, "worktree": link.worktree_path,
        "state": rr.state, "tone": TONES.get(rr.style, ""), "behind": rr.behind,
        "behind_warn": rr.behind >= fetcher.BEHIND_WARN, "pr": rr.pr, "pr_url": rr.pr_url, "merged": rr.merged,
        "fetch_error": rr.fetch_error,
    }


def _row(r: view.Row) -> dict:
    ws = r.ws
    return {
        "key": ws.key, "ticket": ws.ticket, "name": ws.name, "label": ws.label, "status": r.status,
        "status_text": r.status_text, "since": ws.archived_at or ws.status_at, "folder": ws.folder,
        "intake": r.intake, "drift": ws.drift, "all_merged": r.all_merged, "base_ci": r.base_ci,
        "repos": [_repo(rr) for rr in r.repos],
    }


def state() -> dict:
    """Everything the dashboard shows, in the TUI's order: needs you, working, your turn, intake, stopped."""
    tmux.prune_views()
    rows = view.load(include_archived=True)
    active = [r for r in rows if r.status != db.ARCHIVED]
    oks = [rr.fetched_ok_at for r in active for rr in r.repos]
    return {
        "workstreams": [_row(r) for r in active],
        "archived": [_row(r) for r in rows if r.status == db.ARCHIVED],
        "origin": {"repos": len(oks), "checked_at": None if None in oks or not oks else min(oks)},
        # client pid -> the session it shows, so the app notices when tmux moved its terminal (C-b s, say)
        "clients": {str(pid): session for pid, session in tmux.client_sessions().items()},
    }


class Server:
    def __init__(self, out: TextIO):
        self.out = out
        self.lock = threading.Lock()  # one message per line, whichever thread sends it
        self.wake = threading.Event()  # push the state now instead of at the next tick
        self.done = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="wm-request")
        self.last_state: dict | None = None
        self.locks: dict[str, threading.Lock] = {}  # per workstream: one change at a time (an add-repo during an
        self.locks_lock = threading.Lock()  # archive would leave a worktree behind)

    def send(self, msg: dict) -> None:
        line = json.dumps(msg, separators=(",", ":")) + "\n"
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
        for fn, seconds in ((self.push_state, None), (self.refresh_prs, PR_REFRESH_SECONDS),
                            (self.fetch_bases, FETCH_SECONDS)):
            threading.Thread(target=self._every, args=(fn, seconds), daemon=True).start()
        while line := stdin.readline():
            if line.strip():
                self.pool.submit(self.call, line)
        self.done.set()
        self.pool.shutdown(wait=True)  # an archive or rebase in flight runs to the end

    def _every(self, fn, seconds: float | None) -> None:
        while not self.done.is_set():
            try:
                fn()
            except Exception:  # noqa: BLE001 — keep the loop alive; the next tick tries again
                traceback.print_exc()
            if seconds is None:  # the state loop: every STATE_SECONDS, or sooner when woken
                self.wake.wait(STATE_SECONDS)
                self.wake.clear()
            else:
                self.done.wait(seconds)

    def push_state(self) -> None:
        data = state()
        if data != self.last_state:
            self.last_state = data
            self.send({"event": "state", "data": data})

    def call(self, line: str) -> None:
        try:
            req = json.loads(line)
            rid, method, params = req.get("id"), req.get("method", ""), req.get("params") or {}
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
        return state()

    def rpc_repos(self) -> dict:
        return {"repos": [r.name for r in discover(get_config())]}

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
            label, style = view.git_state(r.link, r.status, r.pr, after_pr=r.after_pr, in_base=r.merged)
            repos.append({"repo": r.link.repo, "branch": r.link.branch, "state": label,
                          "tone": TONES.get(style, ""), "pr": gh.describe(r.pr), "blockers": r.blockers})
        return {"label": ws.label, "repos": repos, "blocked": any(r.blockers for r in reports)}

    def rpc_archive(self, key: str, force: bool = False) -> dict:
        with self.locked(key):
            return {"log": workstream.archive(key, force=force)}

    def rpc_unarchive(self, key: str) -> dict:
        with self.locked(key):
            return {"log": workstream.unarchive(key)}

    def rpc_focus(self, active: bool) -> dict:
        """The app came to the front or went to the back: hooks notify only when nobody is looking (tmux.watched)."""
        tmux.set_app_active(active)
        return {}

    def rpc_refresh(self) -> dict:
        self.refresh_prs()
        return {"failed": self.fetch_bases(max_age=0)}

    def rpc_diff(self, key: str) -> dict:
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
        return {"key": ws.key, "repos": repos}

    def rpc_attach(self, key: str) -> dict:
        """argv for the app's terminal: a tmux client on the workstream's app view."""
        ws = workstream.require(db.connect(), key)
        if not workstream.session_alive(ws):
            raise workstream.WorkstreamError(f"{ws.key} is stopped; resume it first")
        return {"argv": tmux.attach_command(tmux.ensure_view(ws.key))}

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
    Server(out).serve(sys.stdin)
