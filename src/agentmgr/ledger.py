"""The brief's structured half: questions for the user, standing rules, facts, what changed, and what Claude is doing.

TASK.md stays the prose brief (goal, context, done when). Everything a person acts on or corrects lives here, in
`.wm/ledger.json` at the workstream root, so the app can show it as cards and edit it without parsing Claude's
markdown. Three writers share the file:

- Claude, through `wm ask / fact / rule / changed / state / step` (pre-approved on the claude command line);
- the hooks: the turn counter, the end-of-turn nudge, and handing the user's answers and edits to Claude;
- the app (through `wm app-server`): answers, edits, side discussions.

Every write takes an exclusive lock and replaces the file in one step, so two writers never lose each other's change
and a reader never sees half a file. After each write `.wm/BRIEF.md` is rendered from it: CLAUDE.md imports that, so
the brief is in context from the first message and comes back after a compaction or a resume.

Turns: the counter goes up when the user sends a message (UserPromptSubmit). Whatever Claude records during that turn
carries its number, so the app can flag "new this turn" until the next message.

The inbox: answers and edits made in the app wait there until a hook hands them to Claude (as extra context on the
next prompt or tool call, or by keeping Claude going when it is about to stop), so they never appear as chat text.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

FOLDER = ".wm"
FILE = "ledger.json"
BRIEF = "BRIEF.md"
BRIEF_IMPORT = f"{FOLDER}/{BRIEF}"

DECISION, ASSUMPTION, BLOCKER = "decision", "assumption", "blocker"
KINDS = (DECISION, ASSUMPTION, BLOCKER)
OPEN, ANSWERED, DROPPED = "open", "answered", "dropped"
YOU, FOUND, ASSUMED = "you", "found", "assumed"
SOURCES = (YOU, FOUND, ASSUMED)
OWNERS = ("claude", "you", "job")
STEP_STATES = ("todo", "doing", "done")
PHASE_STATES = ("todo", "doing", "done", "skipped")
JOB = "job"  # a step can wait on a running job (a monitor, a background command): wm sees those in the transcript

CHANGES_IN_BRIEF = 12  # the most recent changes BRIEF.md lists; the app shows them all
CHANGES_KEPT = 200


class LedgerError(Exception):
    pass


def empty() -> dict:
    return {"version": 1, "turn": 0, "questions": [], "rules": [], "facts": [], "changes": [],
            "state": {"where": None, "turn": 0, "at": None}, "steps": [], "phases": [], "nudged": 0, "inbox": [],
            "threads": [], "background": None}


def folder(ws_path: Path) -> Path:
    return Path(ws_path) / FOLDER


def path(ws_path: Path) -> Path:
    return folder(ws_path) / FILE


def load(ws_path: Path) -> dict:
    """The ledger, or an empty one. A file that doesn't parse reads as empty rather than failing a hook."""
    try:
        data = json.loads(path(ws_path).read_text())
    except (OSError, ValueError):
        return empty()
    base = empty()
    base.update({k: v for k, v in data.items() if k in base})
    return base


@contextmanager
def edit(ws_path: Path) -> Iterator[dict]:
    """Read, change and write the ledger under an exclusive lock; BRIEF.md is rendered from the result."""
    d = folder(ws_path)
    d.mkdir(exist_ok=True)
    with open(d / ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = load(ws_path)
        yield data
        data["changes"] = data["changes"][-CHANGES_KEPT:]
        _replace(path(ws_path), json.dumps(data, indent=1, ensure_ascii=False) + "\n")
        _replace(d / BRIEF, render(data))


def _replace(target: Path, text: str) -> None:
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, target)


def _next_id(items: list[dict], prefix: str) -> str:
    n = max((int(i["id"][len(prefix):]) for i in items if str(i.get("id", "")).startswith(prefix)
             and i["id"][len(prefix):].isdigit()), default=0)
    return f"{prefix}{n + 1}"


def _find(items: list[dict], item_id: str, what: str) -> dict:
    for item in items:
        if item["id"].upper() == item_id.upper():
            return item
    known = ", ".join(i["id"] for i in items) or "none yet"
    raise LedgerError(f"no {what} {item_id} (known: {known})")


def _stamp(data: dict, item: dict, by: str) -> dict:
    item["turn"] = data["turn"]
    item["by"] = by
    item["at"] = time.time()
    return item


def _change(data: dict, text: str, by: str) -> None:
    data["changes"].append(_stamp(data, {"text": text}, by))


# ---------------------------------------------------------------------------- written by Claude


def parse_option(text: str) -> dict:
    """`"label | what it does"` → option. The effect is what makes a question answerable on its own."""
    label, _, effect = text.partition("|")
    if not label.strip():
        raise LedgerError(f"option {text!r} has no label")
    return {"label": label.strip(), "effect": effect.strip()}


def ask(ws_path: Path, title: str, context: str, options: list[str], recommended: int | None,
        if_no_answer: str, kind: str = DECISION) -> str:
    """A question for the user. Returns its id (Q1, Q2, …)."""
    if kind not in KINDS:
        raise LedgerError(f"kind must be one of {', '.join(KINDS)}")
    opts = [parse_option(o) for o in options]
    if kind != BLOCKER and len(opts) < 2:
        raise LedgerError("a decision or assumption needs at least two --option (\"label | what it does\")")
    if recommended is not None and not 1 <= recommended <= len(opts):
        raise LedgerError(f"--recommended must be between 1 and {len(opts)}")
    if not if_no_answer.strip():
        raise LedgerError("--if-no-answer is required: what you'll do if the user doesn't answer, or that you'll wait")
    for i, o in enumerate(opts, 1):
        o["recommended"] = i == recommended
    with edit(ws_path) as data:
        qid = _next_id(data["questions"], "Q")
        data["questions"].append(_stamp(data, {
            "id": qid, "kind": kind, "title": title.strip(), "context": context.strip(), "options": opts,
            "if_no_answer": if_no_answer.strip(), "status": OPEN, "answer": None, "revised": False,
            "drop_reason": None}, "claude"))
    return qid


def revise(ws_path: Path, qid: str, title: str | None = None, context: str | None = None,
           options: list[str] | None = None, recommended: int | None = None, if_no_answer: str | None = None) -> None:
    """Change an open question, e.g. after a side discussion showed it was the wrong question."""
    with edit(ws_path) as data:
        q = _find(data["questions"], qid, "question")
        if q["status"] != OPEN:
            raise LedgerError(f"{q['id']} is already {q['status']}")
        if title:
            q["title"] = title.strip()
        if context:
            q["context"] = context.strip()
        if options:
            q["options"] = [parse_option(o) for o in options]
        if recommended is not None or options:
            for i, o in enumerate(q["options"], 1):
                o["recommended"] = i == recommended
        if if_no_answer:
            q["if_no_answer"] = if_no_answer.strip()
        q["revised"] = True
        _stamp(data, q, "claude")
        _change(data, f"{q['id']} changed: {q['title']}", "claude")


def drop(ws_path: Path, qid: str, reason: str) -> None:
    with edit(ws_path) as data:
        q = _find(data["questions"], qid, "question")
        q["status"], q["drop_reason"] = DROPPED, reason.strip()
        _stamp(data, q, "claude")
        _change(data, f"Dropped {q['id']} ({q['title']}): {reason.strip()}", "claude")


def fact(ws_path: Path, text: str, source: str = FOUND, examples: list[str] | None = None,
         update: str | None = None, remove: str | None = None, question: str | None = None) -> str:
    """`question`: the assumption question that checks this fact; answering it turns the fact into "you said"."""
    if source not in SOURCES:
        raise LedgerError(f"source must be one of {', '.join(SOURCES)}")
    with edit(ws_path) as data:
        if remove:
            f = _find(data["facts"], remove, "fact")
            data["facts"].remove(f)
            _change(data, f"Removed fact: {f['text']}", "claude")
            return f["id"]
        if update:
            f = _find(data["facts"], update, "fact")
            f["text"], f["source"], f["new"] = text.strip(), source, False
            if examples is not None:
                f["examples"] = examples
            _stamp(data, f, "claude")
            return f["id"]
        if question:
            question = _find(data["questions"], question, "question")["id"]
        fid = _next_id(data["facts"], "F")
        data["facts"].append(_stamp(data, {"id": fid, "text": text.strip(), "source": source,
                                           "examples": examples or [], "new": True, "question": question}, "claude"))
        return fid


def rule(ws_path: Path, text: str, update: str | None = None, remove: str | None = None, by: str = "claude") -> str:
    with edit(ws_path) as data:
        if remove:
            old = _find(data["rules"], remove, "rule")["text"]
            _change(data, f"Removed rule: {old}", by)
        return _rule(data, text, update, remove, by)


def _rule(data: dict, text: str, update: str | None, remove: str | None, by: str) -> str:
    if remove:
        r = _find(data["rules"], remove, "rule")
        data["rules"].remove(r)
        return r["id"]
    if update:
        r = _find(data["rules"], update, "rule")
        r["text"], r["new"] = text.strip(), False
        _stamp(data, r, by)
        return r["id"]
    rid = _next_id(data["rules"], "R")
    data["rules"].append(_stamp(data, {"id": rid, "text": text.strip(), "new": True}, by))
    return rid


def changed(ws_path: Path, text: str) -> None:
    with edit(ws_path) as data:
        _change(data, text.strip(), "claude")


def state(ws_path: Path, where: str) -> None:
    """Where the work stands, in one sentence: what the Status card leads with."""
    if not where.strip():
        raise LedgerError("say where things stand, in one sentence")
    with edit(ws_path) as data:
        data["state"] = {"where": " ".join(where.split()), "turn": data["turn"], "at": time.time()}


def _check_waits(data: dict, waits: list[str]) -> list[str]:
    out = []
    for w in waits:
        w = w.strip()
        if w.lower() == JOB:
            out.append(JOB)
        elif w[:1].upper() == "Q":
            out.append(_find(data["questions"], w, "question")["id"])
        elif w[:1].upper() == "S":
            out.append(_find(data["steps"], w, "step")["id"])
        else:
            raise LedgerError(f"--waits-on takes a step (S2), a question (Q1) or \"job\", not {w!r}")
    return out


def step(ws_path: Path, text: str | None = None, owner: str | None = None, status: str | None = None,
         waits_on: list[str] | None = None, update: str | None = None, remove: str | None = None,
         by: str = "claude", phase: str | None = None) -> str:
    """Add a step, or change one (`update`): its text, who does it, its status, what it waits on. A new step
    belongs to the plan's current phase unless `phase` says otherwise."""
    if owner is not None and owner not in OWNERS:
        raise LedgerError(f"owner must be one of {', '.join(OWNERS)}")
    if status is not None and status not in STEP_STATES:
        raise LedgerError(f"status must be one of {', '.join(STEP_STATES)}")
    with edit(ws_path) as data:
        if remove:
            st = _find(data["steps"], remove, "step")
            data["steps"].remove(st)
            for other in data["steps"]:  # nothing waits on a step that is gone
                other["waits_on"] = [w for w in other["waits_on"] if w != st["id"]]
            return st["id"]
        if update:
            st = _find(data["steps"], update, "step")
        else:
            if not (text or "").strip():
                raise LedgerError("missing the step")
            cur = current_phase(data)
            st = {"id": _next_id(data["steps"], "S"), "text": "", "owner": "claude", "status": "todo",
                  "waits_on": [], "done_at": None, "phase": cur["id"] if cur else None}
            data["steps"].append(st)
        if phase is not None:
            st["phase"] = _find(data["phases"], phase, "phase")["id"] if phase else None
        if text and text.strip():
            st["text"] = " ".join(text.split())
        if owner:
            st["owner"] = owner
        if waits_on is not None:
            st["waits_on"] = _check_waits(data, waits_on)
        if status and status != st["status"]:
            st["status"] = status
            if status == "done":
                st["done_at"] = time.time()
                _change(data, f"Done: {st['text']}", by)
        _stamp(data, st, by)
        return st["id"]


def current_phase(data: dict) -> dict | None:
    """The phase in progress, else the first one not started."""
    phases = data.get("phases") or []
    return (next((p for p in phases if p["status"] == "doing"), None)
            or next((p for p in phases if p["status"] == "todo"), None))


def phase(ws_path: Path, text: str | None = None, owner: str | None = None, status: str | None = None,
          question: str | None = None, before: str | None = None, update: str | None = None,
          remove: str | None = None, by: str = "claude") -> str:
    """The plan: the stages the work goes through, in order (retrain → evaluate → decide → deploy). A phase owned by
    the user is a decision point; `question` links the question that decides it. Starting a phase finishes the one
    in progress before it."""
    if owner is not None and owner not in OWNERS:
        raise LedgerError(f"owner must be one of {', '.join(OWNERS)}")
    if status is not None and status not in PHASE_STATES:
        raise LedgerError(f"status must be one of {', '.join(PHASE_STATES)}")
    with edit(ws_path) as data:
        phases = data.setdefault("phases", [])
        if remove:
            ph = _find(phases, remove, "phase")
            phases.remove(ph)
            for st in data["steps"]:
                if st.get("phase") == ph["id"]:
                    st["phase"] = None
            return ph["id"]
        if update:
            ph = _find(phases, update, "phase")
        else:
            if not (text or "").strip():
                raise LedgerError("missing the phase name")
            ph = {"id": _next_id(phases, "P"), "text": "", "owner": "claude", "status": "todo", "question": None,
                  "done_at": None}
            if before:
                phases.insert(phases.index(_find(phases, before, "phase")), ph)
            else:
                phases.append(ph)
        if text and text.strip():
            ph["text"] = " ".join(text.split())
        if owner:
            ph["owner"] = owner
        if question is not None:
            ph["question"] = _find(data["questions"], question, "question")["id"] if question else None
        if status and status != ph["status"]:
            if status == "doing":
                for other in phases[:phases.index(ph)]:
                    if other["status"] == "doing":
                        other["status"], other["done_at"] = "done", time.time()
            ph["status"] = status
            ph["done_at"] = time.time() if status in ("done", "skipped") else None
            if status in ("doing", "done", "skipped"):
                verb = {"doing": "Started", "done": "Finished", "skipped": "Skipped"}[status]
                _change(data, f"{verb} phase: {ph['text']}", by)
        _stamp(data, ph, by)
        return ph["id"]


def blockers(data: dict, st: dict, jobs_running: bool) -> list[dict]:
    """What a step still waits on: open questions, unfinished steps, a running job."""
    out = []
    for w in st.get("waits_on") or []:
        if w == JOB:
            if jobs_running:
                out.append({"id": JOB, "text": "a running job"})
        elif w.startswith("Q"):
            q = next((q for q in data["questions"] if q["id"] == w), None)
            if q and q["status"] == OPEN:
                out.append({"id": w, "text": q["title"]})
        else:
            other = next((o for o in data["steps"] if o["id"] == w), None)
            if other and other["status"] != "done":
                out.append({"id": w, "text": other["text"]})
    return out


# ---------------------------------------------------------------------------- written by hooks


def begin_turn(ws_path: Path) -> None:
    if not path(ws_path).exists():
        return  # workstreams that never used the ledger stay without one
    with edit(ws_path) as data:
        data["turn"] += 1
        data["background"] = None  # a new turn: the last stop's list of running work is out of date


def record_background(ws_path: Path, tasks: list[dict], crons: list[dict]) -> None:
    """What Claude Code said was still running when Claude stopped (the Stop hook's `background_tasks` and
    `session_crons`). The transcript can miss work it has no marker for; this list is authoritative at that moment
    and replaced at the next stop."""
    if not path(ws_path).exists():
        return
    keep = [{"id": str(t.get("id") or ""), "kind": str(t.get("type") or "task"),
             "text": " ".join(str(t.get("description") or t.get("command") or t.get("type") or "").split())[:200],
             "status": str(t.get("status") or "")} for t in tasks if isinstance(t, dict)]
    wakes = [{"id": str(c.get("id") or ""), "kind": "wake-up",
              "text": "a scheduled wake-up" + ("" if c.get("recurring") else f" ({c.get('schedule')})"
                                                if c.get("schedule") else ""),
              "status": "scheduled"} for c in crons if isinstance(c, dict)]
    with edit(ws_path) as data:
        data["background"] = {"at": time.time(), "tasks": keep + wakes}


def should_nudge(ws_path: Path, tools_this_turn: int) -> bool:
    """Once per turn that did real work: Claude hasn't said where things stand since the user's last message."""
    if tools_this_turn < 3 or not path(ws_path).exists():
        return False
    data = load(ws_path)
    if data["state"]["turn"] >= data["turn"] or data["nudged"] >= data["turn"]:
        return False
    with edit(ws_path) as data:
        data["nudged"] = data["turn"]
    return True


def take_inbox(ws_path: Path) -> str | None:
    """The user's answers and edits not yet given to Claude, as one message; the inbox is emptied."""
    if not path(ws_path).exists() or not load(ws_path)["inbox"]:
        return None
    with edit(ws_path) as data:
        items, data["inbox"] = data["inbox"], []
    if not items:
        return None
    lines = ["[wm] The user updated the brief in the Workstreams app (already recorded in .wm/BRIEF.md; don't record "
             "these again):"]
    lines += [f"- {i['text']}" for i in items]
    lines.append("Take these into account now. If an answer changes your plan, say so in one line and record it with "
                 "`wm changed`.")
    return "\n".join(lines)


def inbox_size(ws_path: Path) -> int:
    return len(load(ws_path)["inbox"]) if path(ws_path).exists() else 0


# ---------------------------------------------------------------------------- written by the app


def answer(ws_path: Path, answers: list[dict]) -> list[str]:
    """`answers`: {"id": "Q1", "option": 0} or {"id": "Q3", "text": "use 8 instead"}. Returns the ids answered."""
    done = []
    with edit(ws_path) as data:
        for a in answers:
            q = _find(data["questions"], str(a.get("id", "")), "question")
            if q["status"] != OPEN:
                continue  # answered twice from two windows, or dropped meanwhile
            option, text = a.get("option"), (a.get("text") or "").strip()
            if option is not None:
                if not 0 <= int(option) < len(q["options"]):
                    raise LedgerError(f"{q['id']} has no option {option}")
                chosen = q["options"][int(option)]["label"]
            elif text:
                chosen = text
            else:
                raise LedgerError(f"no answer given for {q['id']}")
            q["status"], q["answer"] = ANSWERED, {"option": option, "text": text or None, "label": chosen}
            _stamp(data, q, "you")
            data["inbox"].append({"text": f"{q['id']} ({q['title']}) answered: {chosen}", "at": time.time()})
            _change(data, f"You answered {q['id']}: {chosen}", "you")
            # an assumption the user confirmed or corrected is now something they said
            if q["kind"] == ASSUMPTION:
                for f in data["facts"]:
                    if f["source"] == ASSUMED and f.get("question") == q["id"]:
                        f["source"] = YOU
                        _stamp(data, f, "you")
            done.append(q["id"])
    return done


def user_edit(ws_path: Path, kind: str, item_id: str | None, text: str | None, remove: bool = False) -> str:
    """The user adds, edits or removes a rule or fact in the app. Edits count as something the user said."""
    if kind not in ("rule", "fact"):
        raise LedgerError("kind must be rule or fact")
    text = (text or "").strip()
    if not remove and not text:
        raise LedgerError("empty text")
    with edit(ws_path) as data:
        if kind == "rule":
            if item_id:
                old = _find(data["rules"], item_id, "rule")["text"]
                rid = _rule(data, text, None if remove else item_id, item_id if remove else None, "you")
                note = f"Removed rule {rid}: {old}" if remove else f"Changed rule {rid} from \"{old}\" to \"{text}\""
            else:
                rid = _rule(data, text, None, None, "you")
                note = f"New rule {rid}: {text}"
        else:
            if item_id:
                f = _find(data["facts"], item_id, "fact")
                old = f["text"]
                if remove:
                    data["facts"].remove(f)
                    note = f"Removed fact {f['id']}: {old}"
                else:
                    f["text"], f["source"], f["new"] = text, YOU, False
                    _stamp(data, f, "you")
                    note = f"Corrected fact {f['id']} from \"{old}\" to \"{text}\""
            else:
                fid = _next_id(data["facts"], "F")
                data["facts"].append(_stamp(data, {"id": fid, "text": text, "source": YOU, "examples": [],
                                                   "new": True}, "you"))
                note = f"New fact {fid}: {text}"
        data["inbox"].append({"text": note, "at": time.time()})
        _change(data, f"You: {note[0].lower()}{note[1:]}", "you")
    return note


def user_step(ws_path: Path, sid: str, status: str) -> str:
    """The user ticks off (or reopens) a step in the app; Claude hears about it like an answer."""
    sid = step(ws_path, status=status, update=sid, by="you")
    with edit(ws_path) as data:
        st = _find(data["steps"], sid, "step")
        note = f"Step {sid} ({st['text']}) marked {'done' if status == 'done' else 'not done'} by the user"
        data["inbox"].append({"text": note, "at": time.time()})
    return note


# ---------------------------------------------------------------------------- side discussions


def open_thread(ws_path: Path, anchor: dict) -> str:
    """A side discussion about one question ({"question": "Q2"}) or a quoted piece of the chat ({"quote": …})."""
    with edit(ws_path) as data:
        if q := anchor.get("question"):
            _find(data["questions"], q, "question")
            for t in data["threads"]:
                if t["anchor"].get("question") == q:
                    return t["id"]
        tid = _next_id(data["threads"], "S")
        data["threads"].append({"id": tid, "anchor": anchor, "fork": None, "messages": [], "busy": False,
                                "error": None, "at": time.time()})
        return tid


def thread_post(ws_path: Path, tid: str, role: str, text: str, busy: bool, fork: str | None = None,
                error: str | None = None) -> None:
    with edit(ws_path) as data:
        t = _find(data["threads"], tid, "side discussion")
        if text:
            t["messages"].append({"role": role, "text": text, "at": time.time()})
        t["busy"], t["error"] = busy, error
        if fork:
            t["fork"] = fork


def close_thread(ws_path: Path, tid: str) -> None:
    with edit(ws_path) as data:
        data["threads"] = [t for t in data["threads"] if t["id"].upper() != tid.upper()]


def thread(ws_path: Path, tid: str) -> dict:
    return _find(load(ws_path)["threads"], tid, "side discussion")


# ---------------------------------------------------------------------------- views


def for_app(ws_path: Path, jobs_running: bool = False) -> dict:
    """What the app's Status and Brief tabs show. Answered and dropped questions stay out; the change log records
    them. Each step comes with what it still waits on."""
    data = load(ws_path)
    steps = [st | {"blocked_by": blockers(data, st, jobs_running)} for st in data["steps"]]
    return {
        "turn": data["turn"],
        "questions": [q for q in data["questions"] if q["status"] == OPEN],
        "rules": data["rules"], "facts": data["facts"],
        "changes": list(reversed(data["changes"])),
        "state": data["state"], "steps": steps, "phases": _phases_for_app(data),
        "inbox": len(data["inbox"]),
        "threads": data["threads"],
        "exists": path(ws_path).exists(),
    }


def _phases_for_app(data: dict) -> list[dict]:
    """Each phase with its question's status and answer, so a decision point shows what was decided."""
    out = []
    cur = current_phase(data)
    for ph in data.get("phases") or []:
        q = next((q for q in data["questions"] if q["id"] == ph.get("question")), None)
        out.append(ph | {"current": cur is not None and ph["id"] == cur["id"],
                         "question_open": bool(q and q["status"] == OPEN),
                         "decided": (q.get("answer") or {}).get("label") if q and q["status"] == ANSWERED else None})
    return out


def render(data: dict) -> str:
    """BRIEF.md: what Claude reads back after a compaction or a resume."""
    out = ["# Brief (generated by wm from .wm/ledger.json — change it with the wm commands, not by editing)", ""]
    if data["state"]["where"]:
        out += ["## Where we are", "", data["state"]["where"], ""]
    if data.get("phases"):
        out += ["## Plan (phases, in order)", ""]
        for ph in data["phases"]:
            gate = f", decided by {ph['question']}" if ph.get("question") else ""
            out.append(f"- {ph['id']} [{ph['status']}, {ph['owner']}{gate}]: {ph['text']}")
        out.append("")
    if data["steps"]:
        out += ["## Steps", ""]
        for st in data["steps"]:
            waits = f" (waits on {', '.join(st['waits_on'])})" if st["waits_on"] and st["status"] != "done" else ""
            ph = f" ({st['phase']})" if st.get("phase") else ""
            out.append(f"- {st['id']}{ph} [{st['status']}, {st['owner']}]: {st['text']}{waits}")
        out.append("")
    out += ["## Rules (set by the user unless marked; they hold until changed)", ""]
    out += [f"- {r['id']}: {r['text']}" + (" (proposed by Claude)" if r.get("by") == "claude" else "")
            for r in data["rules"]] or ["(none)"]
    out += ["", "## Facts", ""]
    labels = {YOU: "user said", FOUND: "found", ASSUMED: "ASSUMED, unconfirmed"}
    for f in data["facts"]:
        out.append(f"- {f['id']} [{labels.get(f['source'], f['source'])}]: {f['text']}")
        out += [f"  - e.g. {e}" for e in f.get("examples") or []]
    if not data["facts"]:
        out.append("(none)")
    open_q = [q for q in data["questions"] if q["status"] == OPEN]
    answered = [q for q in data["questions"] if q["status"] == ANSWERED]
    out += ["", "## Open questions (waiting on the user)", ""]
    for q in open_q:
        out.append(f"- {q['id']} ({q['kind']}): {q['title']} — if no answer: {q['if_no_answer']}")
    if not open_q:
        out.append("(none)")
    if answered:
        out += ["", "## Answered", ""]
        out += [f"- {q['id']}: {q['title']} → {q['answer']['label']}" for q in answered]
    if data["changes"]:
        out += ["", f"## Recent changes (last {CHANGES_IN_BRIEF})", ""]
        out += [f"- {c['text']}" for c in data["changes"][-CHANGES_IN_BRIEF:]]
    return "\n".join(out) + "\n"
