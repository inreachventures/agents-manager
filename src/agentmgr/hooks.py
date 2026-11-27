"""`wm hook <event>`: called by Claude Code with the hook payload on stdin.

Must be fast and must never break the session: any unexpected error exits 0 silently.

Each event runs core's checks (guard, drift) first; if core refuses, that is the answer. Otherwise the plugins that
list the event run in name order, then core applies its status change, then one combined output goes to Claude Code.
In an auxiliary session (`AGENTMGR_AUX=<plugin>`) core runs only its checks and only that plugin is called.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path

from . import db, fetcher, gh, gitops, guard, naming, notify, plugins, tmux
from .config import get_config
from .plugin import HookContext, HookResult

PROMPT_OPTION = "@wm_prompt"  # shown in the pane's top border by tmux.conf
PROMPT_MAX = 500

NEEDS_YOU_NOTIFICATIONS = {"permission_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"}

AUX_ENV = "AGENTMGR_AUX"  # names the plugin whose auxiliary session this is
PLUGIN_BUDGET = 2.0  # seconds a plugin's hook handler may take before it is logged


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


def _transition(conn, ws: db.Workstream, status: str, reason: str | None = None, quiet: bool = False) -> None:
    """Set the status; notify when the session starts waiting on you and you aren't looking. `quiet` (a wait on
    something Claude started) never notifies and counts as seen, so it is not on the dock badge."""
    if ws.status == status and ws.status_reason == reason:
        return
    db.set_status(conn, ws.key, status, reason)
    checked_at = time.time()
    if status in (db.NEEDS_YOU, db.YOUR_TURN) and (quiet or tmux.watched(ws.key)):
        db.mark_seen(conn, ws.key, checked_at)  # you're looking as it starts waiting: not on the dock badge
    elif not quiet and (status == db.NEEDS_YOU or (status == db.YOUR_TURN and ws.status == db.WORKING)):
        msg = reason or ("needs your input" if status == db.NEEDS_YOU else "finished, your turn")
        notify.notify(ws.label, msg)  # nobody is looking at this session


def scratch_dir(ws: db.Workstream) -> Path:
    """Claude Code's temp folder for this workstream; each session's scratchpad lives inside it."""
    return Path("/tmp") / f"claude-{os.getuid()}" / naming.claude_slug(ws.path)


def _context(conn, ws: db.Workstream, payload: dict, protected: list[tuple[str, Path]]) -> guard.Context:
    cfg = get_config()
    cwd = Path(payload.get("cwd") or ws.folder).resolve()
    return guard.Context(
        ws_key=ws.key,
        folder=ws.path.resolve(),
        cwd=cwd,
        code_roots=list(cfg.code_roots),
        repos=list(cfg.repos),
        branches={Path(r.worktree_path).name: r.branch for r in db.list_repos(conn, ws.key)},
        writable=[cfg.memory_dir, scratch_dir(ws)],
        protected=protected,
    )


def _one_line(text: str) -> str:
    # "#" is special in tmux formats; "##" renders as a literal "#"
    return " ".join(text.split())[:PROMPT_MAX].replace("#", "##")


def _show_prompt(ws: db.Workstream, prompt: str) -> None:
    if prompt.strip() and tmux.available():
        tmux.set_window_option(ws.key, PROMPT_OPTION, _one_line(prompt))


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


def on_session_start(conn, ws: db.Workstream, payload: dict) -> str:
    """Status, CLAUDE.md refresh and prompt bar; returns the live state for Claude."""
    source = payload.get("source")
    # Not on compaction: auto-compaction happens mid-turn, while Claude is still working. (A /compact typed by the user
    # fires no UserPromptSubmit or Stop either, so an idle session stays "your turn".)
    if source != "compact":
        _transition(conn, ws, db.YOUR_TURN, None)
    if source in ("compact", "clear"):
        # Claude Code re-reads CLAUDE.md from disk now: bring its repo imports up to date (a repo may have gained a
        # CLAUDE.md since it was attached). If this lands after that read, the next compaction or resume picks it up.
        _regenerate(conn, ws)
    if payload.get("transcript_path"):  # resumed session: show the prompt the history ends with
        _show_prompt(ws, last_prompt(Path(payload["transcript_path"])))
    lines = [f"[wm] Live state of workstream {ws.label} (key {ws.key}):"]
    links = db.list_repos(conn, ws.key)
    errors = fetcher.fetch_stale(fetcher.SESSION_START_MAX_AGE, timeout=5, ws_key=ws.key)
    prs, branch_prs = db.get_prs(conn, ws.key), db.get_branch_prs(conn, ws.key)
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
        prs_text = gh.repo_prs(link, prs.get(link.repo), branch_prs.get(link.repo, [])).describe()
        lines.append(f"- {Path(link.worktree_path).name}/: {state}; {prs_text}")
    if not links:
        lines.append("- no repos attached yet")
    if not (ws.path / "TASK.md").exists() or not ws.name or not links:
        lines.append("Intake is not complete — follow the Intake section of CLAUDE.md before changing code.")
    return "\n".join(lines)


def _regenerate(conn, ws: db.Workstream) -> None:
    """Without failing the hook: the live state printed after it is what Claude most needs after a compaction."""
    try:
        from . import workstream  # here, not at the top: it pulls in rapidfuzz, and every tool call runs a hook

        with _plugin_output():  # regenerating calls plugins' CLAUDE.md handlers
            workstream.regenerate(conn, ws)
    except Exception as e:  # noqa: BLE001
        _log(f"SessionStart: regenerate: {type(e).__name__}: {e}")


def check_tool(conn, ws: db.Workstream, payload: dict, aux: str | None) -> str | None:
    """PreToolUse guard: the reason to refuse the call, or None. Plugins' protected folders are read from their
    manifests (never by importing them); an auxiliary session may write its own plugin's."""
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    try:
        manifests, _ = plugins.discover()
    except Exception as e:  # noqa: BLE001 - a broken plugin setup must not switch the guard off
        _log(f"PreToolUse: plugins: {type(e).__name__}: {e}")
        manifests = []
    protected = [(m.name, Path(".wm") / m.name) for m in manifests if m.protect and m.name != aux]
    ctx = _context(conn, ws, payload, protected)
    if tool in guard.FILE_TOOLS:
        return guard.check_file_tool(tool, tool_input, ctx)
    if tool == "Bash":
        return guard.check_bash(tool_input.get("command", ""), ctx)
    if tool in ("Agent", "Task"):
        return guard.check_agent(tool_input, ctx)
    if tool == "EnterWorktree":
        return guard.check_enter_worktree(ctx)
    return None


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


def check_drift(conn, ws: db.Workstream, payload: dict) -> str | None:
    """PostToolUse after Bash: record drift and return the block reason while there is any."""
    if payload.get("tool_name") != "Bash":
        return None
    problems = drift(conn, ws)
    text = "; ".join(problems) or None
    if text != ws.drift:
        db.update_workstream(conn, ws.key, drift=text)
    if not problems:
        return None
    return (
        f"[wm] Workstream {ws.key} drifted: {text}. Restore it now (e.g. switch the worktree back to its "
        "branch, `git worktree remove` anything you created) and don't repeat the command."
    )


def _working(conn, ws: db.Workstream) -> None:
    if ws.status != db.WORKING:
        _transition(conn, ws, db.WORKING)


def on_notification(conn, ws: db.Workstream, payload: dict) -> None:
    kind = payload.get("notification_type") or ""
    message = payload.get("message") or ""
    if kind in NEEDS_YOU_NOTIFICATIONS or (not kind and "permission" in message.lower()):
        _transition(conn, ws, db.NEEDS_YOU, message or kind)
    elif kind == "idle_prompt" and ws.status not in (db.NEEDS_YOU, db.YOUR_TURN):
        _transition(conn, ws, db.YOUR_TURN, None)


# ---------------------------------------------------------------------------- plugins


def merge(results: list[HookResult]) -> HookResult:
    """One result from the plugins' results, in order: contexts joined, the first deny, block and waiting_on."""
    def first(field: str) -> str | None:
        return next((v for r in results if (v := getattr(r, field))), None)

    contexts = [r.context for r in results if r.context]
    return HookResult(context="\n\n".join(contexts) or None, deny=first("deny"), block=first("block"),
                      waiting_on=first("waiting_on"))


def _valid(result: object) -> bool:
    return isinstance(result, HookResult) and all(
        v is None or isinstance(v, str) for v in (result.context, result.deny, result.block, result.waiting_on))


def _plugin_output() -> contextlib.AbstractContextManager:
    """While plugin code runs: what it prints goes to stderr, as the hook's stdout is read by Claude Code (JSON, or
    context Claude sees)."""
    return contextlib.redirect_stdout(sys.stderr)


def run_plugins(conn, ws: db.Workstream, event: str, payload: dict, aux: str | None) -> list[HookResult]:
    """Call the handlers of the plugins that list `event` (only the `aux` plugin in its auxiliary session), each
    isolated: a plugin that raises, is slow or returns nonsense is logged and left out."""
    manifests, _ = plugins.discover()
    declared = {m.name for m in manifests if event in m.hooks and aux in (None, m.name)}
    if not declared:
        return []  # nothing imported
    with _plugin_output():  # importing a plugin runs its code
        lps = [lp for lp in plugins.loaded(event=event) if lp.manifest.name in declared]
    if missing := declared - {lp.manifest.name for lp in lps}:
        for s in plugins.skipped():
            if s.name in missing:
                plugins.log(f"plugin {s.name}: skipped: {s.reason}")
    info = plugins.workstream_info(ws, db.list_repos(conn, ws.key))
    results = []
    for lp in lps:
        fn = lp.api.registered.hooks.get(event)
        if fn is None:
            continue
        what = f"hook {event}"
        with _plugin_output():
            result = plugins.call(lp, what, fn, HookContext(ws=info, event=event, payload=payload,
                                                            aux=aux is not None), budget=PLUGIN_BUDGET)
        if result is None:
            continue
        if not _valid(result):
            plugins.log(f"plugin {lp.manifest.name}: {what}: returned {result!r}, not a HookResult of text fields")
            continue
        results.append(result)
    return results


# ---------------------------------------------------------------------------- dispatch


def _status(conn, ws: db.Workstream, event: str, payload: dict, result: HookResult) -> str | None:
    """Core's status change for the event, after the plugins ran; SessionStart returns the live state."""
    if event == "SessionStart":
        return on_session_start(conn, ws, payload)
    if event == "UserPromptSubmit":
        _transition(conn, ws, db.WORKING)
        _show_prompt(ws, payload.get("prompt") or "")
    elif event == "PreToolUse":
        if not result.deny:
            _working(conn, ws)
    elif event == "PostToolUse":
        _working(conn, ws)  # a permission prompt was answered and the tool ran: back to working
    elif event == "Notification":
        on_notification(conn, ws, payload)
    elif event == "Stop" and not result.block:  # a block keeps Claude going: no change
        if result.waiting_on:
            _transition(conn, ws, db.YOUR_TURN, result.waiting_on, quiet=True)
        else:
            _transition(conn, ws, db.YOUR_TURN)
    elif event == "SessionEnd":
        _transition(conn, ws, db.STOPPED, payload.get("reason"))
    return None


def _output(event: str, result: HookResult, text: str | None) -> None:
    """One output in Claude Code's shape for the event."""
    if event == "SessionStart":
        if parts := [p for p in (text, result.context) if p]:
            print("\n\n".join(parts))
        return
    if event == "PreToolUse":
        if result.deny:
            _deny(result.deny)
        return
    out: dict = {}
    if event in ("PostToolUse", "Stop") and result.block:
        out.update(decision="block", reason=result.block)
    if event in ("UserPromptSubmit", "PostToolUse") and result.context:
        out["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": result.context}
    if out:
        _emit(out)


def dispatch(event: str, payload: dict) -> None:
    conn = db.connect()
    ws = _find(conn, payload)
    if ws is None:
        return  # not a managed session
    aux = os.environ.get(AUX_ENV) or None
    if event == "PreToolUse" and (reason := check_tool(conn, ws, payload, aux)):
        _deny(reason)
        return
    if event == "PostToolUse" and (reason := check_drift(conn, ws, payload)):
        if not aux:
            _working(conn, ws)
        _emit({"decision": "block", "reason": reason})
        return
    try:
        results = run_plugins(conn, ws, event, payload, aux)
    except Exception as e:  # noqa: BLE001 - core's status change and output still happen
        _log(f"{event}: plugins: {type(e).__name__}: {e}")
        results = []
    result = merge(results)
    text = None if aux else _status(conn, ws, event, payload, result)
    _output(event, result, text)


def main(event: str) -> None:
    plugins.set_logger(_log)
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        dispatch(event, payload)
    except Exception as e:  # noqa: BLE001
        _log(f"{event}: {type(e).__name__}: {e}")


def _log(line: str) -> None:
    try:
        with (get_config().home / "hooks.log").open("a") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass
