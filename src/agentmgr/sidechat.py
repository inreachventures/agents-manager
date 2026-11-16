"""Side discussions: questions about one card in Needs you, or about a quoted piece of the chat, that stay out of the
main conversation.

Each discussion runs as a fork of the workstream's conversation (`claude -p --resume <id> --fork-session`): it knows
everything said so far, but nothing it says comes back to the main session. Follow-ups resume the fork. It has the
same tools as the main session (the user's choice), with two limits:

- wm refuses to start one while the main session is working, and the app shows the main session as busy while one
  runs, so two copies of Claude don't change the same worktree at once;
- its hooks run the guardrails but don't touch the workstream's status (AGENTMGR_SIDE is set): a side discussion
  finishing must not tell the dashboard the main session finished.

A discussion about a question may end with Claude rewording it (`wm ask --revise`) or dropping it (`wm ask --drop`);
the app sees that through the ledger like any other change.
"""

from __future__ import annotations

import json
import os
import subprocess

from . import db, ledger, templates
from .config import get_config

SIDE_ENV = "AGENTMGR_SIDE"
TIMEOUT = 15 * 60


class SideError(Exception):
    pass


def _prompt(ws: db.Workstream, t: dict, text: str) -> str:
    """The first message of a fork says what the side discussion is about; follow-ups are the user's words."""
    if t["fork"]:
        return text
    anchor = t["anchor"]
    rules = (f"This is a side discussion in the Workstreams app, separate from the main conversation of {ws.label}: "
             "nothing said here reaches the main session. Answer briefly and plainly, about this only. Don't carry on "
             "with the main task here unless the user asks you to.")
    if qid := anchor.get("question"):
        q = next((q for q in ledger.load(ws.path)["questions"] if q["id"] == qid), None)
        if q is None:
            raise SideError(f"{qid} no longer exists")
        options = "\n".join(f"  {i}. {o['label']}: {o['effect']}" for i, o in enumerate(q["options"], 1))
        return (f"{rules}\n\nThe user is discussing open question {qid} from the brief:\n"
                f"{q['title']}\n{q['context']}\nOptions:\n{options}\nIf no answer: {q['if_no_answer']}\n\n"
                f"If the discussion shows the question should be worded differently or have other options, change "
                f"it with `wm ask {ws.key} --revise {qid} …`. If it shows the question isn't needed, run "
                f"`wm ask {ws.key} --drop {qid} --reason \"…\"`. Say in one line when you did either.\n\n"
                f"The user says: {text}")
    quote = anchor.get("quote", "")
    return (f"{rules}\n\nThe user selected this part of the conversation:\n\"\"\"\n{quote}\n\"\"\"\n\n"
            f"Their question about it: {text}")


def command(ws: db.Workstream, t: dict, prompt: str) -> list[str]:
    cfg = get_config()
    cmd = [cfg.claude_cmd, "-p", prompt, "--output-format", "json"]
    cmd += ["--resume", t["fork"]] if t["fork"] else ["--resume", ws.session_id, "--fork-session"]
    cmd += ["--allowedTools", ",".join(templates.allowed_tools(ws, cfg.memory_dir))]
    cmd += ["--settings", json.dumps({"autoMemoryDirectory": str(cfg.memory_dir)})]
    cmd += ["--permission-mode", "acceptEdits"]  # no one is there to answer a prompt; Bash outside the list is denied
    return cmd


def ask(ws: db.Workstream, tid: str, text: str) -> None:
    """Post the user's message and run Claude on it; blocks until Claude answers. Run it in a worker thread."""
    t = ledger.thread(ws.path, tid)
    if t["busy"]:
        raise SideError("Claude is still answering this side discussion")
    prompt = _prompt(ws, t, text)
    ledger.thread_post(ws.path, tid, "user", text, busy=True)
    env = {**os.environ, SIDE_ENV: "1", "AGENTMGR_WS": ws.key}
    try:
        res = subprocess.run(command(ws, t, prompt), cwd=ws.path, env=env, capture_output=True, text=True,
                             timeout=TIMEOUT)
        out = json.loads(res.stdout or "{}")
    except (subprocess.TimeoutExpired, ValueError, OSError) as e:
        ledger.thread_post(ws.path, tid, "claude", "", busy=False, error=f"Claude didn't answer: {e}")
        return
    if res.returncode != 0 or out.get("is_error"):
        reason = out.get("result") or res.stderr.strip().splitlines()[-1:] or [f"exit {res.returncode}"]
        ledger.thread_post(ws.path, tid, "claude", "", busy=False,
                           error=f"Claude didn't answer: {reason if isinstance(reason, str) else reason[0]}")
        return
    ledger.thread_post(ws.path, tid, "claude", str(out.get("result") or "").strip(), busy=False,
                       fork=out.get("session_id"))


def is_side() -> bool:
    return bool(os.environ.get(SIDE_ENV))
