"""The workstream's conversation as messages the app can render, read from Claude Code's transcript (JSONL).

The terminal stays the place where Claude runs and asks for permissions; this is a second, read-only view of the same
conversation, so text can be laid out, selected and quoted. Tool calls are folded into one line per run of calls
("Read 3 files · ran 2 commands"), as the mockup's "What I did".

Transcripts grow to many megabytes, so `Reader` keeps what it parsed and on the next call reads only the bytes added.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import naming

MAX_MESSAGES = 400  # the app shows the most recent ones; older history stays in the terminal's scrollback
MAX_EVENTS = 60  # the Now tab's "this turn" list
TOOL_VERBS = {"Read": "read", "Edit": "edited", "Write": "wrote", "MultiEdit": "edited", "NotebookEdit": "edited",
              "Bash": "ran", "Grep": "searched", "Glob": "searched", "WebFetch": "fetched", "WebSearch": "searched",
              "Agent": "started", "Task": "started"}


def path_for(ws_folder: Path, session_id: str) -> Path:
    return Path.home() / ".claude" / "projects" / naming.claude_slug(Path(ws_folder)) / f"{session_id}.jsonl"


def _detail(inp: dict, limit: int = 120) -> str:
    detail = inp.get("file_path") or inp.get("command") or inp.get("pattern") or inp.get("url") \
        or inp.get("description") or inp.get("query") or inp.get("subject") or ""
    detail = " ".join(str(detail).split())
    return detail[:limit - 1] + "…" if len(detail) > limit else detail


def _tool_line(name: str, inp: dict) -> str:
    """One short line per tool call, for the folded "What I did" list."""
    detail = _detail(inp)
    return f"{name}: {detail}" if detail else name


def _epoch(ts) -> float | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return " ".join(str(content or "").split())


BLOCKED_MARKERS = ("permission", "denied", "blocked", "rejected", "doesn't want to proceed")
NOTICE = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
MONITOR_DEFAULT_SECONDS = 30 * 60


class Jobs:
    """Background work Claude started and is waiting on: monitors, background commands, background subagents.

    Started by a tool call (Monitor; Bash or Agent with run_in_background), whose result names the task id. Ended by
    a task notification with a final status, by TaskStop, or for a monitor when it expires. While one runs, Claude
    having stopped doesn't mean it's the user's turn."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self._pending: dict[str, dict] = {}  # tool_use id -> job, until its result gives the task id
        self._ended: set[str] = set()  # every id seen ending, including jobs started without a marker we know

    def tool_use(self, block: dict, at: float | None) -> None:
        name, inp = block.get("name"), block.get("input") or {}
        if name == "TaskStop":
            self._end(str(inp.get("task_id") or inp.get("shell_id") or ""), "stopped", at)
            return
        if name == "Monitor":
            kind, timeout = "monitor", (inp.get("timeout_ms") or MONITOR_DEFAULT_SECONDS * 1000) / 1000
            if inp.get("persistent"):
                timeout = None
        elif name in ("Bash", "Agent", "Task") and inp.get("run_in_background"):
            kind, timeout = ("command" if name == "Bash" else "agent"), None
        else:
            return
        text = " ".join(str(inp.get("description") or inp.get("command") or name).split())
        self._pending[block.get("id", "")] = {"id": None, "kind": kind, "text": text[:200], "status": "running",
                                              "started_at": at, "ended_at": None, "timeout": timeout}

    def result(self, tool_use_id: str, result, at: float | None) -> None:
        job = self._pending.pop(tool_use_id, None)
        if job is None or not isinstance(result, dict):
            return
        tid = result.get("taskId") or result.get("backgroundTaskId") or result.get("agentId")
        if tid:
            job["id"] = str(tid)
            self.jobs[job["id"]] = job
            if job["id"] in self._ended:  # its notice was written before the tool result (a quick job)
                job["status"], job["ended_at"] = "completed", at

    def notice(self, text: str, at: float | None) -> None:
        for body in NOTICE.findall(text):
            tid = re.search(r"<task-id>(.*?)</task-id>", body)
            if not tid:
                continue
            status = re.search(r"<status>(.*?)</status>", body)
            if status and status.group(1).strip() in ("completed", "failed", "killed"):
                self._end(tid.group(1).strip(), status.group(1).strip(), at)
            elif "Monitor expired" in body or "Monitor stopped" in body:
                self._end(tid.group(1).strip(), "expired", at)

    def ended(self, tid: str) -> bool:
        return tid in self._ended

    def _end(self, tid: str, status: str, at: float | None) -> None:
        self._ended.add(tid)
        job = self.jobs.get(tid)
        if job and job["status"] == "running":
            job["status"], job["ended_at"] = status, at

    def running(self, now: float | None = None) -> list[dict]:
        now = now or time.time()
        out = []
        for job in self.jobs.values():
            if job["status"] != "running":
                continue
            if job["timeout"] and job["started_at"] and now > job["started_at"] + job["timeout"]:
                continue  # a monitor past its timeout has expired, even if the notice isn't written yet
            out.append(job)
        return out

    def feed(self, e: dict) -> None:
        """One transcript entry."""
        at = _epoch(e.get("timestamp"))
        content = (e.get("message") or {}).get("content")
        if e.get("type") == "assistant" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    self.tool_use(b, at)
        elif e.get("type") == "user":
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        self.result(b.get("tool_use_id", ""), e.get("toolUseResult"), at)
                    elif isinstance(b, dict) and b.get("type") == "text":
                        self.notice(b.get("text", ""), at)
            elif isinstance(content, str):
                self.notice(content, at)
        # newer Claude Code versions write the notice where it's queued, not as a user message
        if e.get("type") == "queue-operation" and isinstance(e.get("content"), str):
            self.notice(e["content"], at)
        attachment = e.get("attachment")
        if isinstance(attachment, dict) and isinstance(attachment.get("prompt"), str):
            self.notice(attachment["prompt"], at)


JOB_HINTS = (b"run_in_background", b'"Monitor"', b"task-notification", b'"TaskStop"', b"backgroundTaskId",
             b'"taskId"', b'"agentId"')


def running_jobs(transcript: Path) -> list[dict]:
    """For hooks: the jobs still running, reading only the lines that can start or end one."""
    jobs = Jobs()
    try:
        with transcript.open("rb") as f:
            for line in f:
                if any(h in line for h in JOB_HINTS):
                    try:
                        jobs.feed(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        return []
    return jobs.running()


def tools_since_prompt(transcript: Path, tail: int = 4_000_000) -> int:
    """How many tool calls Claude made since the user's last message (read from the end of the file)."""
    try:
        with transcript.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - tail))
            lines = f.read().splitlines()
    except OSError:
        return 0
    count = 0
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        content = (e.get("message") or {}).get("content")
        if e.get("type") == "user" and not e.get("isMeta") and isinstance(content, str) \
                and content.strip() and not content.lstrip().startswith("<"):
            break
        if e.get("type") == "assistant" and isinstance(content, list):
            count += sum(1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use")
    return count


_readers: dict[Path, Reader] = {}
_readers_lock = threading.Lock()


def reader_for(transcript: Path) -> Reader:
    """One incremental reader per transcript, shared by the dashboard state, the chat view and the Status tab."""
    with _readers_lock:
        r = _readers.get(transcript)
        if r is None:
            r = _readers[transcript] = Reader(transcript)
        return r


def _summary(tools: list[str]) -> str:
    counts: dict[str, int] = {}
    for t in tools:
        name = t.split(":", 1)[0]
        counts[name] = counts.get(name, 0) + 1
    parts = []
    for name, n in counts.items():
        verb = TOOL_VERBS.get(name)
        if name == "Read":
            parts.append(f"read {n} file{'s' * (n > 1)}")
        elif name == "Bash":
            parts.append(f"ran {n} command{'s' * (n > 1)}")
        elif verb in ("edited", "wrote"):
            parts.append(f"{verb} {n} file{'s' * (n > 1)}")
        else:
            parts.append(f"{name} ×{n}" if n > 1 else name)
    text = " · ".join(parts)
    return text[:1].upper() + text[1:]


@dataclass
class Reader:
    path: Path
    offset: int = 0
    messages: list[dict] = field(default_factory=list)
    pending_tools: list[str] = field(default_factory=list)  # tool calls since the last text, not yet emitted
    # The Now tab: what happened since the user's last message, tool calls with how they ended
    events: list[dict] = field(default_factory=list)
    turn_started: float | None = None
    jobs: Jobs = field(default_factory=Jobs)
    lock: threading.Lock = field(default_factory=threading.Lock)
    _calls: dict[str, dict] = field(default_factory=dict)  # tool_use id -> its event, until its result comes

    def activity(self) -> dict:
        self.read()
        return {"started_at": self.turn_started, "events": self.events[-MAX_EVENTS:]}

    def read(self) -> list[dict]:
        with self.lock:  # the state loop and request threads share readers
            return self._read()

    def running_jobs(self) -> list[dict]:
        self.read()
        return self.jobs.running()

    def _read(self) -> list[dict]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:  # rewritten (a rewind): start over
            self.offset, self.messages, self.pending_tools = 0, [], []
            self.events, self.turn_started, self._calls, self.jobs = [], None, {}, Jobs()
        if size > self.offset:
            with self.path.open("rb") as f:
                f.seek(self.offset)
                chunk = f.read(size - self.offset)
            end = chunk.rfind(b"\n") + 1  # a line still being written is read next time
            for line in chunk[:end].splitlines():
                self._entry(line)
            self.offset += end
            self.messages = self.messages[-MAX_MESSAGES:]
        out = list(self.messages)
        if self.pending_tools:
            out.append(self._tools_message("pending"))
        return out

    def _tools_message(self, mid: str) -> dict:
        return {"id": f"tools-{mid}", "role": "tools", "text": _summary(self.pending_tools),
                "tools": list(self.pending_tools), "at": None}

    def _flush_tools(self, mid: str) -> None:
        if self.pending_tools:
            self.messages.append(self._tools_message(mid))
            self.pending_tools = []

    def _entry(self, line: bytes) -> None:
        try:
            e = json.loads(line)
        except ValueError:
            return
        if e.get("isSidechain"):
            return
        self.jobs.feed(e)  # before the filter below: job notices also come as queue entries and attachments
        if e.get("type") not in ("user", "assistant"):
            return
        mid, at = e.get("uuid") or str(len(self.messages)), e.get("timestamp")
        content = (e.get("message") or {}).get("content")
        if e.get("isCompactSummary"):
            self._flush_tools(mid)
            self.messages.append({"id": mid, "role": "note", "text": "Conversation compacted", "at": at})
            return
        if e["type"] == "user":
            if e.get("isMeta"):
                return
            if isinstance(content, list):
                results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
                for b in results:
                    self._finish(b)
                if results:
                    return
                content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
            if not isinstance(content, str) or not content.strip() or content.lstrip().startswith("<"):
                return
            self._flush_tools(mid)
            role = "wm" if content.startswith("[wm]") else "user"
            self.messages.append({"id": mid, "role": role, "text": content.strip(), "at": at})
            self.events, self._calls, self.turn_started = [], {}, _epoch(at)  # a new turn
            return
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name, inp = block.get("name", "tool"), block.get("input") or {}
                self.pending_tools.append(_tool_line(name, inp))
                # a shell command's own description reads better than the command; the command goes under it
                described = name == "Bash" and inp.get("description")
                event = {"kind": "tool", "tool": name, "status": "running", "at": _epoch(at), "note": None,
                         "text": " ".join(str(inp["description"]).split()) if described else _detail(inp, 300),
                         "command": _detail({"command": inp.get("command")}, 300) if described else None}
                self.events.append(event)
                if block.get("id"):
                    self._calls[block["id"]] = event
            elif block.get("type") == "text" and block.get("text", "").strip():
                self._flush_tools(mid + "-t")
                self.messages.append({"id": f"{mid}-{len(self.messages)}", "role": "claude",
                                      "text": block["text"].strip(), "at": at})
                text = " ".join(block["text"].split())
                self.events.append({"kind": "text", "tool": None, "status": "done", "at": _epoch(at), "note": None,
                                    "text": text[:400] + ("…" if len(text) > 400 else ""), "command": None})

    def _finish(self, block: dict) -> None:
        """A tool result: mark its call done, failed or blocked (a denied permission or a guard)."""
        event = self._calls.pop(block.get("tool_use_id", ""), None)
        if event is None:
            return
        if not block.get("is_error"):
            event["status"] = "done"
            return
        text = _result_text(block)
        event["status"] = "blocked" if any(m in text.lower() for m in BLOCKED_MARKERS) else "failed"
        event["note"] = text[:200] + ("…" if len(text) > 200 else "")


def merge_background(running: list[dict], jobs: Jobs | None, background: dict | None) -> list[dict]:
    """The transcript's running jobs plus what Claude Code listed as in flight at the last stop, minus any the
    transcript has since seen end. Both sources name tasks by the same id."""
    listed = {t["id"] for t in (background or {}).get("tasks") or []}
    # a job that started before the last stop but isn't on Claude Code's list has ended, whatever the transcript says
    out = [j for j in running if not background or j["id"] in listed
           or (j["started_at"] or 0) > background["at"]]
    seen = {j["id"] for j in out}
    for t in (background or {}).get("tasks") or []:
        if t["id"] in seen or (jobs is not None and jobs.ended(t["id"])):
            continue
        out.append({"id": t["id"], "kind": t["kind"], "text": t["text"] or t["kind"], "status": "running",
                    "started_at": None, "ended_at": None, "timeout": None})
        seen.add(t["id"])
    return out
