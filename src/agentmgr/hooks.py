"""`wm hook <event>`: called by Claude Code with the hook payload on stdin.

Must be fast and must never break the session: any unexpected error exits 0 silently.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import db, fetcher, gh, gitops, guard, notify, tmux
from .config import get_config

PROMPT_OPTION = "@wm_prompt"  # shown in the pane's top border by tmux.conf
PROMPT_MAX = 500

NEEDS_YOU_NOTIFICATIONS = {"permission_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"}


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj))


def _deny(reason: str) -> None:
    _emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                  "permissionDecisionReason": reason}})


def _find(conn, payload: dict) -> db.Workstream | None:
    ws = db.find_by_session(conn, payload.get("session_id", ""))
    if ws is None and payload.get("cwd"):
        ws = db.find_by_folder(conn, str(Path(payload["cwd"]).resolve()))
    return ws if ws and ws.status != db.ARCHIVED else None


def _transition(conn, ws: db.Workstream, status: str, reason: str | None = None) -> None:
    if ws.status == status and ws.status_reason == reason:
        return
    db.set_status(conn, ws.key, status, reason)
    if status == db.NEEDS_YOU or (status == db.YOUR_TURN and ws.status == db.WORKING):
        if not tmux.clients(ws.key):  # nobody is looking at this session
            msg = reason or ("needs your input" if status == db.NEEDS_YOU else "finished, your turn")
            notify.notify(ws.label, msg)


def _context(conn, ws: db.Workstream, payload: dict) -> guard.Context:
    cwd = Path(payload.get("cwd") or ws.folder).resolve()
    return guard.Context(
        ws_key=ws.key,
        folder=ws.path.resolve(),
        cwd=cwd,
        code_roots=list(get_config().code_roots),
        branches={Path(r.worktree_path).name: r.branch for r in db.list_repos(conn, ws.key)},
    )


def _one_line(text: str) -> str:
    # "#" is special in tmux formats; "##" renders as a literal "#"
    return " ".join(text.split())[:PROMPT_MAX].replace("#", "##")


def _show_prompt(ws: db.Workstream, prompt: str) -> None:
    if prompt.strip() and tmux.available():
        tmux.set_session_option(ws.key, PROMPT_OPTION, _one_line(prompt))


def last_prompt(transcript: Path) -> str:
    """Text of the last message the user typed in a Claude Code transcript (JSONL), or ""."""
    last = ""
    try:
        lines = transcript.read_text().splitlines()
    except OSError:
        return ""
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") != "user" or entry.get("isMeta"):
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, list):  # tool results, or a prompt with attachments
            if any(b.get("type") == "tool_result" for b in content if isinstance(b, dict)):
                continue
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        if isinstance(content, str) and content.strip() and not content.lstrip().startswith("<"):
            last = content
    return last


# ---------------------------------------------------------------------------- events


def on_session_start(conn, ws: db.Workstream, payload: dict) -> None:
    _transition(conn, ws, db.YOUR_TURN, None)
    if payload.get("transcript_path"):  # resumed session: show the prompt the history ends with
        _show_prompt(ws, last_prompt(Path(payload["transcript_path"])))
    lines = [f"[wm] Live state of workstream {ws.label} (key {ws.key}):"]
    links = db.list_repos(conn, ws.key)
    errors = fetcher.fetch_stale(fetcher.SESSION_START_MAX_AGE, timeout=5, ws_key=ws.key)
    prs = db.get_prs(conn, ws.key)
    for link in links:
        try:
            st = gitops.status(Path(link.worktree_path), link.base)
            state = f"on {st.branch or 'DETACHED'}, {st.summary()}, {st.ahead_of_base} commits ahead of {link.base}"
            if st.branch != link.branch:
                state += f"  ⚠ expected branch {link.branch}"
            if errors.get(link.repo_path):
                state += f" (could not fetch {link.base}, numbers may be stale)"
            elif st.behind_base >= fetcher.BEHIND_WARN:
                state += f" — consider rebasing onto {link.base}"
        except gitops.GitError as e:
            state = f"unavailable ({e})"
        lines.append(f"- {Path(link.worktree_path).name}/: {state}; {gh.describe(prs.get(link.repo))}")
    if not links:
        lines.append("- no repos attached yet")
    if not (ws.path / "TASK.md").exists() or not ws.name or not links:
        lines.append("Intake is not complete — follow the Intake section of CLAUDE.md before changing code.")
    print("\n".join(lines))


def on_pre_tool_use(conn, ws: db.Workstream, payload: dict) -> None:
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    ctx = _context(conn, ws, payload)
    reason = None
    if tool in guard.FILE_TOOLS:
        reason = guard.check_file_tool(tool, tool_input, ctx)
    elif tool == "Bash":
        reason = guard.check_bash(tool_input.get("command", ""), ctx)
    elif tool in ("Agent", "Task"):
        reason = guard.check_agent(tool_input, ctx)
    elif tool == "EnterWorktree":
        reason = guard.check_enter_worktree(ctx)
    if reason:
        _deny(reason)
        return
    if ws.status != db.WORKING:
        _transition(conn, ws, db.WORKING)


def drift(conn, ws: db.Workstream) -> list[str]:
    problems = []
    folder = ws.path.resolve()
    for link in db.list_repos(conn, ws.key):
        wt = Path(link.worktree_path)
        if not wt.exists():
            problems.append(f"{wt.name}/ is missing")
            continue
        branch = gitops.current_branch(wt)
        if branch != link.branch:
            problems.append(f"{wt.name}/ is on {branch or 'a detached HEAD'} instead of {link.branch}")
        for other in gitops.worktrees(Path(link.repo_path)):
            p = other.path.resolve()
            if p != wt.resolve() and (p == folder or folder in p.parents):
                problems.append(f"unexpected worktree {p} in {link.repo}")
    attached = {Path(link.worktree_path).name for link in db.list_repos(conn, ws.key)}
    for child in ws.path.iterdir():
        if child.name not in attached and (child / ".git").exists():
            problems.append(
                f"{child.name}/ is a git checkout not attached to the workstream "
                f"(remove it and run `wm add-repo {ws.key} <repo>`)"
            )
    return problems


def on_post_tool_use(conn, ws: db.Workstream, payload: dict) -> None:
    # a permission prompt was answered and the tool ran: back to working
    if ws.status != db.WORKING:
        _transition(conn, ws, db.WORKING)
    if payload.get("tool_name") != "Bash":
        return
    problems = drift(conn, ws)
    text = "; ".join(problems) or None
    if text != ws.drift:
        db.update_workstream(conn, ws.key, drift=text)
    if problems:
        _emit({"decision": "block", "reason": (
            f"[wm] Workstream {ws.key} drifted: {text}. Restore it now (e.g. switch the worktree back to its "
            "branch, `git worktree remove` anything you created) and don't repeat the command."
        )})


def on_notification(conn, ws: db.Workstream, payload: dict) -> None:
    kind = payload.get("notification_type") or ""
    message = payload.get("message") or ""
    if kind in NEEDS_YOU_NOTIFICATIONS or (not kind and "permission" in message.lower()):
        _transition(conn, ws, db.NEEDS_YOU, message or kind)
    elif kind == "idle_prompt" and ws.status not in (db.NEEDS_YOU, db.YOUR_TURN):
        _transition(conn, ws, db.YOUR_TURN, None)


def dispatch(event: str, payload: dict) -> None:
    conn = db.connect()
    ws = _find(conn, payload)
    if ws is None:
        return  # not a managed session
    if event == "SessionStart":
        on_session_start(conn, ws, payload)
    elif event == "UserPromptSubmit":
        _transition(conn, ws, db.WORKING)
        _show_prompt(ws, payload.get("prompt") or "")
    elif event == "PreToolUse":
        on_pre_tool_use(conn, ws, payload)
    elif event == "PostToolUse":
        on_post_tool_use(conn, ws, payload)
    elif event == "Notification":
        on_notification(conn, ws, payload)
    elif event == "Stop":
        _transition(conn, ws, db.YOUR_TURN)
    elif event == "SessionEnd":
        _transition(conn, ws, db.STOPPED, payload.get("reason"))


def main(event: str) -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        dispatch(event, payload)
    except Exception as e:  # noqa: BLE001
        try:
            log = get_config().home / "hooks.log"
            with log.open("a") as f:
                f.write(f"{event}: {type(e).__name__}: {e}\n")
        except Exception:  # noqa: BLE001
            pass
