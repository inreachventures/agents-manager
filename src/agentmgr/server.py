"""`wm app-server`: the backend of the native macOS app, speaking JSON lines on stdin/stdout.

The app sends `{"id": 1, "method": "archive", "params": {"key": "WS-7"}}` and gets back `{"id": 1, "result": ...}` or
`{"id": 1, "error": "..."}`. Unprompted, the server sends `{"event": "state", "data": ...}` whenever the dashboard
state changes (checked every 2s, like the TUI; every 10s while the app is in the background) and
`{"event": "notice", "data": {"level": ..., "message": ...}}`.

The app is only a view: every change goes through the workstream service, exactly as from the CLI and the TUI, so
worktrees, branches and guardrails stay wm's job. Requests run in threads, so a slow archive doesn't hold up the rest.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TextIO

from . import db, diffs, fetcher, gh, gitops, ledger, sessioncolor, sidechat, tmux, transcript, view, workstream
from .config import Config, get_config, save_paths
from .naming import NamingError
from .repos import RepoMatchError, discover

STATE_SECONDS = 2
BACKGROUND_STATE_SECONDS = 10  # while the app is behind other apps, when only its dock badge shows
BACKGROUND_GIT_MAX_AGE = 60  # view.GIT_MAX_AGE while the app is in the background
PR_REFRESH_SECONDS = 60
COLOR_WAIT_SECONDS = 10  # after typing /color, how long to show the picked colour while Claude's transcript lacks it
FETCH_SECONDS = 30  # each repo is actually fetched at most every fetcher.DASHBOARD_MAX_AGE
EXPECTED = (workstream.WorkstreamError, NamingError, RepoMatchError, gitops.GitError, tmux.TmuxError,
            ledger.LedgerError, sidechat.SideError)
# Requests that change nothing the dashboard shows, so no state push after them: the app asks for a diff every 3s,
# and pushing the state after each one kept the state loop (git in every repo) running without a pause.
QUIET = frozenset({"state", "repos", "rebase_plan", "archive_report", "setup", "scan", "diff", "focus", "brief",
                   "transcript"})

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


def waiting(ws: db.Workstream, data: dict, jobs: list[dict]) -> dict:
    """Who the workstream is waiting on, in one word and one line: Claude, a job, you, or nobody (done).

    Claude having stopped isn't the same as "your turn": while a monitor or background job it started is running,
    it's waiting on that job and will pick up when it reports back."""
    if ws.status in (db.ARCHIVED, db.STOPPED):
        return {"on": ws.status, "text": ws.status}
    if ws.status == db.NEEDS_YOU:
        return {"on": "you", "text": ws.status_reason or "waiting on a prompt in the terminal"}
    if ws.status == db.WORKING:
        return {"on": "claude", "text": "Claude is working"}
    questions = [q for q in data["questions"] if q["status"] == ledger.OPEN]
    if questions:
        n = len(questions)
        return {"on": "you", "text": f"{n} question{'s' * (n > 1)} for you"}
    if jobs:
        more = f" (+{len(jobs) - 1} more)" if len(jobs) > 1 else ""
        return {"on": "job", "text": f"waiting on {jobs[0]['text']}{more}"}
    open_steps = [st for st in data["steps"] if st["status"] != "done"]
    yours = [st for st in open_steps if st["owner"] == "you" and not ledger.blockers(data, st, False)]
    if yours:
        return {"on": "you", "text": yours[0]["text"]}
    if data["steps"] and not open_steps:
        return {"on": "done", "text": "all steps done"}
    return {"on": "you", "text": "your turn" if ws.status == db.YOUR_TURN else ws.status}


def _jobs(ws: db.Workstream) -> list[dict]:
    if ws.status in (db.ARCHIVED, db.STOPPED):
        return []  # background work ends with the session
    reader = transcript.reader_for(transcript.path_for(ws.path, ws.session_id))
    running = reader.running_jobs()
    if ws.status == db.WORKING:
        return running  # the stop list describes the last stop, not a turn under way
    return transcript.merge_background(running, reader.jobs, ledger.load(ws.path)["background"])


def _row(r: view.Row, unseen: bool, color: str | None) -> dict:
    ws = r.ws
    example = workstream.is_example(ws) and r.status != db.ARCHIVED
    data = ledger.load(ws.path) if r.status != db.ARCHIVED else ledger.empty()
    wait = waiting(ws, data, _jobs(ws))
    return {
        "key": ws.key, "ticket": ws.ticket, "name": ws.name, "label": ws.label, "status": r.status,
        # waiting on you, and nobody has looked at the session since it started waiting; a session whose Claude
        # waits on its own job isn't waiting on you, so it stays off the dock badge
        "unseen": unseen and wait["on"] == "you",
        "color": color,  # Claude's /color for the session (see sessioncolor), None for its default
        "status_text": "example · ready to archive" if example else r.status_text,
        "since": ws.archived_at or ws.status_at, "folder": ws.folder,
        "intake": r.intake, "drift": ws.drift, "all_merged": r.all_merged, "base_ci": r.base_ci,
        # the sidebar's "N questions" badge, and who it is waiting on (see `waiting`)
        "open_questions": sum(q["status"] == ledger.OPEN for q in data["questions"]),
        "waiting": wait,
        # everything merged and CI passed on base, or no CI there: the TUI's "→ x archive"
        # and nothing is still going on: no job running, no open step or question
        "ready_to_archive": example or (r.all_merged and r.base_ci in ("pass", None) and wait["on"] in ("you", "done")
                                        and not any(st["status"] != "done" for st in data["steps"])
                                        and not any(q["status"] == ledger.OPEN for q in data["questions"])),
        "repos": [_repo(rr) for rr in r.repos],
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

    def color(ws: db.Workstream) -> str | None:
        chosen = colors.get(ws.key)
        return (chosen if chosen in sessioncolor.COLORS else None) if chosen else sessioncolor.current(ws)

    return {
        "workstreams": [_row(r, r.ws.key in unseen, color(r.ws)) for r in active],
        "archived": [_row(r, False, None) for r in rows if r.status == db.ARCHIVED],
        "origin": {"repos": len(oks), "checked_at": None if None in oks or not oks else min(oks)},
        # client pid -> the session it shows, so the app notices when tmux moved its terminal (C-b s, say)
        "clients": {str(pid): session for pid, session in clients.items()},
    }


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


def task_goal(ws_path: Path) -> str | None:
    """The Goal section of TASK.md, which heads the app's Brief panel; None during intake."""
    try:
        text = (ws_path / "TASK.md").read_text()
    except OSError:
        return None
    _, found, rest = text.partition("## Goal")
    if not found:
        return None
    goal = rest.split("\n## ", 1)[0].strip()
    return " ".join(goal.split()) or None


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


class Server:
    def __init__(self, out: TextIO):
        self.out = out
        self.lock = threading.Lock()  # one message per line, whichever thread sends it
        self.wake = threading.Event()  # push the state now instead of at the next tick
        self.done = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="wm-request")
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
        try:
            workstream.repoint_hooks()
        except (workstream.WorkstreamError, OSError):
            traceback.print_exc()
        for fn, seconds in ((self.push_state, None), (self.refresh_prs, PR_REFRESH_SECONDS),
                            (self.fetch_bases, FETCH_SECONDS)):
            threading.Thread(target=self._every, args=(fn, seconds), daemon=True).start()
        while line := stdin.readline():
            if line.strip():
                self.pool.submit(self.call, line)
        self.done.set()
        self.pool.shutdown(wait=True)  # an archive or rebase in flight runs to the end
        with self.typing:  # nor quit halfway through typing a /color, which would leave it in the prompt
            pass

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

    def git_max_age(self) -> float:
        return view.GIT_MAX_AGE if self.app_active else BACKGROUND_GIT_MAX_AGE

    def push_state(self) -> None:
        fresh, self.fresh = self.fresh, False
        try:
            self.apply_colors()
        except Exception:  # noqa: BLE001 — the dashboard goes out regardless
            traceback.print_exc()
        data = state(self.chosen_colors(), git_max_age=0 if fresh else self.git_max_age())
        if data != self.last_state:
            self.last_state = data
            self.send({"event": "state", "data": data})

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
            label, style = view.git_state(r.link, r.status, r.pr, r.after_pr, r.in_base, others=r.others)
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

    # ------------------------------------------------------------------ the brief and the chat

    def rpc_brief(self, key: str, since: str | None = None) -> dict:
        """The Brief panel: questions, now, rules, facts, changes, side discussions. `since` as in rpc_diff."""
        ws = workstream.require(db.connect(), key)
        reader = self.reader(ws)
        jobs = _jobs(ws)
        data = ledger.for_app(ws.path, jobs_running=bool(jobs)) | {
            "goal": task_goal(ws.path), "activity": reader.activity(), "jobs": jobs,
            "waiting": waiting(ws, ledger.load(ws.path), jobs)}
        digest = hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]
        if digest == since:
            return {"key": ws.key, "digest": digest, "unchanged": True}
        return {"key": ws.key, "digest": digest, **data}

    def rpc_brief_answer(self, key: str, answers: list[dict]) -> dict:
        """Record answers from the app. If Claude is waiting for the user, one short line starts its turn and the
        UserPromptSubmit hook hands it the answers; if it is working, the next tool call or its Stop does (hooks.py).
        A permission prompt is left alone: typing into it would answer it."""
        conn = db.connect()
        ws = workstream.require(conn, key)
        ids = ledger.answer(ws.path, answers)
        sent = False
        if ids and ws.status == db.YOUR_TURN and workstream.session_alive(ws):
            with self.typing:  # not while a /color goes in (apply_colors)
                # only at an empty prompt: a draft you started in the terminal would get the line appended. Else
                # the answers wait in the inbox for your next message.
                if sessioncolor.prompt_ready(tmux.capture(ws.key), sessioncolor.vim_keys()):
                    tmux.send_text(ws.key, f"[wm] Answered in the brief: {', '.join(ids)}")
                    sent = True
        return {"answered": ids, "sent": sent}

    def rpc_brief_edit(self, key: str, kind: str, id: str | None = None, text: str | None = None,
                       remove: bool = False) -> dict:
        """The user adds, corrects or removes a rule or fact. It reaches Claude with its next prompt or tool call,
        without starting a turn of its own."""
        ws = workstream.require(db.connect(), key)
        return {"note": ledger.user_edit(ws.path, kind, id, text, remove=remove)}

    def rpc_brief_step(self, key: str, id: str, done: bool) -> dict:
        """The user ticks off a step (their own, usually) or reopens one."""
        ws = workstream.require(db.connect(), key)
        return {"note": ledger.user_step(ws.path, id, "done" if done else "todo")}

    def rpc_side_open(self, key: str, question: str | None = None, quote: str | None = None,
                      message: str | None = None) -> dict:
        ws = workstream.require(db.connect(), key)
        anchor = {"question": question.upper()} if question else {"quote": (quote or "").strip(), "message": message}
        if not question and not anchor["quote"]:
            raise ledger.LedgerError("nothing to discuss: no question and no quote")
        return {"id": ledger.open_thread(ws.path, anchor)}

    def rpc_side_ask(self, key: str, id: str, text: str) -> dict:
        """Start Claude on a side discussion and return at once; the answer shows up in the brief."""
        conn = db.connect()
        ws = workstream.require(conn, key)
        if ws.status == db.WORKING:
            raise sidechat.SideError("Claude is working in the main session; ask again when it stops, so two "
                                     "copies of Claude don't change the same files at once")
        if not text.strip():
            raise sidechat.SideError("empty message")
        if ledger.thread(ws.path, id)["busy"]:
            raise sidechat.SideError("Claude is still answering this side discussion")
        self.pool.submit(self._side, ws, id, text.strip())
        return {}

    def _side(self, ws: db.Workstream, tid: str, text: str) -> None:
        try:
            sidechat.ask(ws, tid, text)
        except Exception as e:  # noqa: BLE001 — the thread shows the error instead of spinning forever
            traceback.print_exc()
            ledger.thread_post(ws.path, tid, "claude", "", busy=False, error=f"{type(e).__name__}: {e}")
        self.wake.set()

    def rpc_side_close(self, key: str, id: str) -> dict:
        ws = workstream.require(db.connect(), key)
        ledger.close_thread(ws.path, id)
        return {}

    def rpc_transcript(self, key: str, since: str | None = None) -> dict:
        """The conversation as messages for the app's chat view. `since` as in rpc_diff."""
        ws = workstream.require(db.connect(), key)
        messages = self.reader(ws).read()
        digest = hashlib.sha1(json.dumps(messages, sort_keys=True).encode()).hexdigest()[:16]
        if digest == since:
            return {"key": ws.key, "digest": digest, "unchanged": True, "messages": []}
        return {"key": ws.key, "digest": digest, "messages": messages}

    def reader(self, ws: db.Workstream) -> transcript.Reader:
        """The workstream's transcript, parsed incrementally; shared by the chat view and the Status tab."""
        return transcript.reader_for(transcript.path_for(ws.path, ws.session_id))

    def rpc_send(self, key: str, text: str) -> dict:
        """A message typed in the app's chat view, sent to Claude as if typed in the terminal (one line: a newline
        would submit what comes before it)."""
        ws = workstream.require(db.connect(), key)
        if not workstream.session_alive(ws):
            raise workstream.WorkstreamError(f"{ws.key} is stopped; resume it first")
        if ws.status == db.NEEDS_YOU:
            raise workstream.WorkstreamError("Claude is waiting on a prompt in the terminal; answer it there first")
        line = " ".join(text.split())
        if not line:
            raise workstream.WorkstreamError("empty message")
        with self.typing:  # not while a /color or an answer line goes in
            if sessioncolor.prompt_text(tmux.capture(ws.key)):
                raise workstream.WorkstreamError("there's a message half-typed in the terminal; send or clear it "
                                                 "there first")
            tmux.send_text(ws.key, line)
        return {}

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
