"""`wm` command line. Thin layer over the workstream service; heavy imports stay lazy
(hooks and the tmux status line run this on every event)."""

from __future__ import annotations

import json
import sys
import time
from typing import Annotated

import typer

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    invoke_without_command=True,
    help="Agents manager: multi-repo Claude Code workstreams.",
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _fail(msg: str) -> None:
    typer.secho(f"error: {msg}", fg="red", err=True)
    raise typer.Exit(1)


def _service():
    from . import workstream

    return workstream


def _run(fn, *args, **kwargs):
    """Call the service, turning expected errors into a clean message."""
    from .gitops import GitError
    from .naming import NamingError
    from .repos import RepoMatchError
    from .tmux import TmuxError
    from .workstream import WorkstreamError

    try:
        return fn(*args, **kwargs)
    except (WorkstreamError, NamingError, RepoMatchError, GitError, TmuxError) as e:
        _fail(str(e))


@app.callback()
def root(ctx: typer.Context) -> None:
    """With no command: open the dashboard."""
    if ctx.invoked_subcommand is None:
        home()


def home() -> None:
    from . import tmux

    if not tmux.available():
        _fail("tmux is not installed (brew install tmux)")
    tmux.ensure_home([tmux.wm_executable(), "dashboard"])
    tmux.open_session(tmux.HOME_SESSION)


# ---------------------------------------------------------------------------- workstreams


@app.command()
def new(
    ticket: Annotated[str | None, typer.Argument(help="Ticket id, e.g. PROJ-313 (optional)")] = None,
    name: Annotated[str | None, typer.Option("--name", "-n", help="3-word name")] = None,
    repo: Annotated[list[str] | None, typer.Option("--repo", "-r", help="Repo to attach (repeatable)")] = None,
    task: Annotated[str | None, typer.Option("--task", "-t", help="Task description (first prompt)")] = None,
    launch: Annotated[bool, typer.Option(help="Start the Claude session")] = True,
    open_: Annotated[bool, typer.Option("--open/--no-open", help="Switch to the session")] = True,
    adopt: Annotated[bool, typer.Option(
        "--adopt", help="Adopt the in-progress work of each --repo from its normal checkout (needs --name)")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="With --adopt: don't ask for confirmation")] = False,
) -> None:
    """Create a workstream. With no arguments you land in Claude and describe the task (voice or text)."""
    svc = _service()
    if adopt and not (name and repo):
        _fail("--adopt needs --name and at least one --repo")
    if adopt:
        ws = _run(svc.new, ticket=ticket, name=name, task=task, launch=False)
        typer.echo(f"created {ws.key} in {ws.folder}")
        for r in repo:
            _adopt(ws.key, r, None, yes)
        if launch:
            _run(svc.start_session, svc.db.get_workstream(svc.db.connect(), ws.key), initial_prompt=task)
    else:
        ws = _run(svc.new, ticket=ticket, name=name, repo_queries=repo, task=task, launch=launch)
        typer.echo(f"created {ws.key} in {ws.folder}")
    if launch and open_:
        svc.open_(ws.key)


@app.command("set")
def set_cmd(
    key: str,
    name: Annotated[str | None, typer.Option("--name", "-n")] = None,
    ticket: Annotated[str | None, typer.Option("--ticket")] = None,
) -> None:
    """Set a workstream's 3-word name and/or ticket id."""
    ws, warnings = _run(_service().set_, key, name=name, ticket=ticket)
    typer.echo(f"{ws.key}: name={ws.name!r} ticket={ws.ticket or '-'}")
    for w in warnings:
        typer.echo(f"note: {w}")


@app.command("add-repo")
def add_repo(
    key: str,
    repo: str,
    branch: Annotated[str | None, typer.Option("--branch", "-b", help="Existing branch to continue")] = None,
    allow_foreign_branch: bool = False,
    adopt: Annotated[bool, typer.Option(
        "--adopt", help="Move in-progress work (current branch + uncommitted changes) from the checkout into the "
                        "workstream")] = False,
    from_: Annotated[str | None, typer.Option(
        "--from", help="With --adopt: the checkout holding the work (default: the main checkout in ~/code)")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="With --adopt: don't ask for confirmation")] = False,
) -> None:
    """Attach a repo: creates the worktree and branch inside the workstream folder."""
    if adopt:
        _adopt(key, repo, from_, yes)
        return
    res = _run(_service().add_repo, key, repo, branch=branch, allow_foreign_branch=allow_foreign_branch)
    link = res.link
    how = f"new branch {link.branch} from {link.base}" if res.created_branch else f"existing branch {link.branch}"
    typer.echo(f"attached {link.repo} → {link.worktree_path} ({how})")
    typer.echo(f"work in {link.worktree_path} (stay on branch {link.branch}); CLAUDE.md repo table updated")
    for n in res.notes:
        typer.echo(f"note: {n}")


def _adopt(key: str, repo: str, source: str | None, yes: bool) -> None:
    svc = _service()
    plan = _run(svc.plan_adopt, key, repo, source=source)
    for line in plan.describe():
        typer.echo(line)
    if not yes:
        if not sys.stdin.isatty():
            _fail("confirm the plan above with the user, then rerun with --yes")
        if not typer.confirm("proceed?", default=True):
            raise typer.Exit(1)
    res = _run(svc.adopt, key, plan)
    link = res.link
    typer.echo(f"attached {link.repo} → {link.worktree_path} (branch {link.branch})")
    typer.echo(f"work in {link.worktree_path} (stay on branch {link.branch}); CLAUDE.md repo table updated")
    for n in res.notes:
        typer.echo(f"note: {n}")


@app.command("remove-repo")
def remove_repo(key: str, repo: str, force: bool = False) -> None:
    """Detach a repo (only if it has no uncommitted changes or commits of its own, unless --force)."""
    _run(_service().remove_repo, key, repo, force=force)
    typer.echo(f"removed {repo} from {key}")


@app.command("open")
def open_cmd(ref: str) -> None:
    """Switch to (or attach) a workstream's session; resumes it if it stopped."""
    svc = _service()
    ws = _run(svc.resolve, svc.db.connect(), ref)
    _run(svc.open_, ws.key)


@app.command()
def resume(ref: str) -> None:
    """Restart a stopped session with the same conversation."""
    svc = _service()
    ws = _run(svc.resolve, svc.db.connect(), ref)
    _run(svc.resume, ws.key)
    typer.echo(f"{ws.key} running")


@app.command()
def archive(
    ref: str,
    force: Annotated[bool, typer.Option(help="Discard uncommitted/unpushed work")] = False,
    keep_branches: Annotated[bool, typer.Option(help="Don't delete merged branches")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    """Stop the session and remove the worktrees (after a safety report)."""
    from . import view

    svc = _service()
    ws = _run(svc.resolve, svc.db.connect(), ref)
    ws, reports = _run(svc.cleanup_report, ws.key)
    typer.echo(f"{ws.label}:")
    for r in reports:
        state, _ = view.git_state(r.link, r.status, r.pr, r.after_pr, r.in_base, others=r.others)
        typer.echo(f"  {r.link.repo:<28} {r.link.branch:<40} {state} · {r.describe_prs()}")
        for b in r.blockers:
            typer.secho(f"    ✗ {b}", fg="red")
    if any(r.blockers for r in reports) and not force:
        _fail("work would be lost; commit/push first or pass --force")
    if not yes and not typer.confirm("archive?", default=True):
        raise typer.Exit(1)
    for line in _run(svc.archive, ws.key, force=force, delete_branches=not keep_branches):
        typer.echo(line)


@app.command()
def rebase(ref: str, yes: Annotated[bool, typer.Option("--yes", "-y")] = False) -> None:
    """Rebase the branches that are behind their base; ones already on origin are force-pushed (with lease)."""
    svc = _service()
    ws = _run(svc.resolve, svc.db.connect(), ref)
    ws, steps = _run(svc.rebase_plan, ws.key)
    typer.echo(f"{ws.label}:")
    for s in steps:
        what = f"skipped: {s.skip}" if s.skip else (
            f"{s.behind} behind {s.link.base} → rebase" + (", then force-push" if s.pushed_at else ""))
        typer.echo(f"  {s.link.repo:<28} {s.link.branch:<40} {what}")
    if all(s.skip for s in steps):
        typer.echo("nothing to rebase")
        return
    if not yes and not typer.confirm("rebase?", default=True):
        raise typer.Exit(1)
    for line in _run(svc.rebase, ws.key):
        typer.echo(line)


@app.command()
def unarchive(
    ref: str,
    open_: Annotated[bool, typer.Option("--open", help="Resume the session and switch to it")] = False,
) -> None:
    """Restore an archived workstream: recreate its worktrees on the same branches."""
    svc = _service()
    ws = _run(svc.resolve, svc.db.connect(), ref, archived=True)
    for line in _run(svc.unarchive, ws.key):
        typer.echo(line)
    if open_:
        _run(svc.open_, ws.key)


STATUS_ICON = {"needs_you": "⚠", "working": "●", "your_turn": "◐", "intake": "✎", "stopped": "○",
               "archived": "·"}


def _ago(ts: float) -> str:
    s = int(time.time() - ts)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit}"
    return f"{s}s"


@app.command("ls")
def ls(
    all_: Annotated[bool, typer.Option("--all", "-a", help="Include archived")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    fetch: Annotated[bool, typer.Option("--fetch", "-f", help="Fetch each repo's base branch first")] = False,
) -> None:
    """List workstreams."""
    from . import fetcher, view

    if fetch:
        for path, err in fetcher.fetch_stale(0).items():
            if err:
                typer.secho(f"could not fetch {path}: {err}", fg="yellow", err=True)

    rows = view.load(include_archived=all_, git_status=True)
    if as_json:
        typer.echo(json.dumps([r.to_dict() for r in rows], indent=2))
        return
    if not rows:
        typer.echo("no workstreams — create one with `wm new`")
        return
    for r in rows:
        typer.echo(f"{STATUS_ICON.get(r.status, '?')} {r.ws.label:<40} {r.status_text:<34} {_ago(r.ws.status_at)}")
        for rr in r.repos:
            typer.echo(f"      {rr.link.repo:<26} {rr.link.branch:<40} {rr.git} · {rr.pr}")


@app.command()
def repos(query: Annotated[str | None, typer.Argument()] = None) -> None:
    """List (or fuzzy-match) repos under the code roots."""
    from . import repos as repos_mod
    from .config import get_config

    found = repos_mod.discover(get_config())
    if query:
        typer.echo(_run(repos_mod.match, query, found).name)
        return
    for r in found:
        typer.echo(f"{r.name:<32} {r.path}")


@app.command()
def branches(repo: str) -> None:
    """List local + remote branches of a repo."""
    from pathlib import Path

    from . import gitops
    from . import repos as repos_mod
    from .config import get_config

    r = _run(repos_mod.match, repo, repos_mod.discover(get_config()))
    names = sorted(set(gitops.local_branches(Path(r.path))) | set(gitops.remote_branches(Path(r.path))))
    typer.echo("\n".join(names))


@app.command()
def doctor() -> None:
    """Check required tools."""
    import shutil
    import subprocess
    from pathlib import Path

    from . import claudetrust
    from .config import get_config

    cfg = get_config()
    ok = True
    for tool, cmd, args, required in (("git", "git", ["--version"], True), ("tmux", "tmux", ["-V"], True),
                                      ("claude", cfg.claude_cmd, ["--version"], True),  # the command sessions run
                                      ("gh", "gh", ["--version"], False)):
        path = shutil.which(cmd)
        if not path:
            ok = ok and not required
            typer.secho(f"{'✗' if required else '!'} {tool}: {cmd} not found", fg="red" if required else "yellow")
            continue
        out = subprocess.run([path, *args], capture_output=True, text=True).stdout.splitlines()
        typer.echo(f"✓ {tool}: {out[0] if out else path}")
    typer.echo(f"  home: {cfg.home}\n  workstreams: {cfg.workstreams_dir}\n  code roots: "
               + ", ".join(map(str, cfg.code_roots))
               + (f"\n  single repos: {', '.join(map(str, cfg.repos))}" if cfg.repos else ""))
    if not claudetrust.is_trusted(cfg.workstreams_dir, Path.home() / claudetrust.STATE_FILE_NAME):
        typer.secho(f"! {cfg.workstreams_dir} is not trusted by Claude Code yet: wm trusts it when it next "
                    "starts a session.", fg="yellow")
    if not shutil.which("wm"):
        typer.secho("! `wm` is not on PATH; Claude sessions call it during intake", fg="yellow")
    if not ok:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------- the brief (written by Claude)

BRIEF_PANEL = "The brief (used by Claude in a workstream)"


def _brief(key: str, fn, *args, **kwargs):
    """Run a ledger write for workstream `key`, turning a bad id or missing field into a clean message."""
    from . import db, ledger

    ws = _run(_service().require, db.connect(), key)
    try:
        return fn(ws.path, *args, **kwargs)
    except ledger.LedgerError as e:
        _fail(str(e))


@app.command(rich_help_panel=BRIEF_PANEL)
def ask(
    key: str,
    question: Annotated[str | None, typer.Argument(help="The question, in the user's words")] = None,
    context: Annotated[str, typer.Option("--context", "-c", help="What it is about and why it matters")] = "",
    option: Annotated[list[str] | None, typer.Option(
        "--option", "-o", help='"label | what this option does to the result" (repeatable)')] = None,
    recommended: Annotated[int | None, typer.Option(help="The option you recommend, from 1")] = None,
    if_no_answer: Annotated[str, typer.Option(help="What you will do if the user doesn't answer")] = "",
    assumption: Annotated[bool, typer.Option("--assumption", help="Checks something you assumed")] = False,
    blocker: Annotated[bool, typer.Option("--blocker", help="Something only the user can do")] = False,
    revise: Annotated[str | None, typer.Option(help="Reword an open question, e.g. Q2")] = None,
    drop: Annotated[str | None, typer.Option(help="Drop an open question that is no longer needed")] = None,
    reason: Annotated[str, typer.Option(help="With --drop: why")] = "",
) -> None:
    """Put a question for the user in the brief (the app shows it under Needs you)."""
    from . import ledger

    if drop:
        if not reason.strip():
            _fail("--drop needs --reason")
        _brief(key, ledger.drop, drop, reason)
        typer.echo(f"dropped {drop.upper()}")
    elif revise:
        _brief(key, ledger.revise, revise, title=question, context=context or None, options=option or None,
               recommended=recommended, if_no_answer=if_no_answer or None)
        typer.echo(f"revised {revise.upper()}")
    else:
        if not question:
            _fail("missing the question")
        kind = ledger.BLOCKER if blocker else ledger.ASSUMPTION if assumption else ledger.DECISION
        qid = _brief(key, ledger.ask, question, context, option or [], recommended, if_no_answer, kind)
        typer.echo(f"{qid} is in the brief; the user answers it in the app")


@app.command(rich_help_panel=BRIEF_PANEL)
def fact(
    key: str,
    text: Annotated[str, typer.Argument()] = "",
    found: Annotated[bool, typer.Option("--found", help="You checked it (default)")] = False,
    assumed: Annotated[bool, typer.Option("--assumed", help="Not checked yet")] = False,
    you: Annotated[bool, typer.Option("--you", help="The user said it")] = False,
    example: Annotated[list[str] | None, typer.Option(help="A real id or row the user can check (repeatable)")] = None,
    question: Annotated[str | None, typer.Option(help="With --assumed: the question that checks it")] = None,
    update: Annotated[str | None, typer.Option(help="Correct fact F<n>")] = None,
    remove: Annotated[str | None, typer.Option(help="Delete fact F<n>")] = None,
) -> None:
    """Record something known about the task, with where it comes from."""
    from . import ledger

    if not remove and not text.strip():
        _fail("missing the fact")
    source = ledger.ASSUMED if assumed else ledger.YOU if you else ledger.FOUND
    fid = _brief(key, ledger.fact, text, source, examples=example, update=update, remove=remove, question=question)
    typer.echo(f"{'removed' if remove else 'recorded'} {fid}")


@app.command(rich_help_panel=BRIEF_PANEL)
def rule(
    key: str,
    text: Annotated[str, typer.Argument()] = "",
    update: Annotated[str | None, typer.Option(help="Change rule R<n>")] = None,
    remove: Annotated[str | None, typer.Option(help="Delete rule R<n>")] = None,
) -> None:
    """Record a rule the user set; it holds until they change it."""
    from . import ledger

    if not remove and not text.strip():
        _fail("missing the rule")
    rid = _brief(key, ledger.rule, text, update=update, remove=remove)
    typer.echo(f"{'removed' if remove else 'recorded'} {rid}")


@app.command(rich_help_panel=BRIEF_PANEL)
def changed(key: str, text: str) -> None:
    """Record what you learned and what it changed: "learned X → changed Y"."""
    from . import ledger

    _brief(key, ledger.changed, text)
    typer.echo("recorded")


@app.command(rich_help_panel=BRIEF_PANEL)
def state(key: str, where: str) -> None:
    """Say where the work stands, in one sentence (the app's Status card leads with it)."""
    from . import ledger

    _brief(key, ledger.state, where)
    typer.echo("recorded")


@app.command(rich_help_panel=BRIEF_PANEL)
def step(
    key: str,
    text: Annotated[str, typer.Argument(help="What has to happen, in plain words")] = "",
    owner: Annotated[str | None, typer.Option(help="claude (default), you, or job")] = None,
    waits_on: Annotated[list[str] | None, typer.Option(
        "--waits-on", help="A step (S2), a question (Q1) or job (repeatable)")] = None,
    doing: Annotated[str | None, typer.Option(help="Mark step S<n> as in progress")] = None,
    done: Annotated[str | None, typer.Option(help="Mark step S<n> as done")] = None,
    update: Annotated[str | None, typer.Option(help="Change step S<n> (text, --owner, --waits-on)")] = None,
    remove: Annotated[str | None, typer.Option(help="Delete step S<n>")] = None,
    phase: Annotated[str | None, typer.Option(help="The plan phase it belongs to (P2); new steps default to the "
                                                   "current phase")] = None,
) -> None:
    """Add or update a step of the work: what happens next, who does it, and what it waits on."""
    from . import ledger

    if done or doing:
        sid = _brief(key, ledger.step, text or None, owner, "done" if done else "doing", waits_on,
                     update=done or doing, phase=phase)
        typer.echo(f"{sid} {'done' if done else 'in progress'}")
    elif remove:
        typer.echo(f"removed {_brief(key, ledger.step, remove=remove)}")
    else:
        sid = _brief(key, ledger.step, text or None, owner, None, waits_on, update=update, phase=phase)
        typer.echo(f"{'updated' if update else 'added'} {sid}")


@app.command(rich_help_panel=BRIEF_PANEL)
def phase(
    key: str,
    text: Annotated[str, typer.Argument(help="The phase, in plain words (e.g. \"Evaluate the new model\")")] = "",
    owner: Annotated[str | None, typer.Option(help="claude (default), you (a decision point), or job")] = None,
    question: Annotated[str | None, typer.Option(help="The question (Q<n>) that decides a decision point")] = None,
    before: Annotated[str | None, typer.Option(help="Insert before phase P<n> instead of at the end")] = None,
    start: Annotated[str | None, typer.Option(help="Start phase P<n> (finishes the one in progress)")] = None,
    done: Annotated[str | None, typer.Option(help="Mark phase P<n> as finished")] = None,
    skip: Annotated[str | None, typer.Option(help="Skip phase P<n> (e.g. no deploy after a \"no\")")] = None,
    update: Annotated[str | None, typer.Option(help="Change phase P<n> (text, --owner, --question)")] = None,
    remove: Annotated[str | None, typer.Option(help="Delete phase P<n>")] = None,
) -> None:
    """Add or move a phase of the plan: the stages the work goes through, in order."""
    from . import ledger

    status = "doing" if start else "done" if done else "skipped" if skip else None
    if status:
        pid = _brief(key, ledger.phase, text or None, owner, status, question, update=start or done or skip)
        typer.echo(f"{pid} {status}")
    elif remove:
        typer.echo(f"removed {_brief(key, ledger.phase, remove=remove)}")
    else:
        pid = _brief(key, ledger.phase, text or None, owner, None, question, before, update=update)
        typer.echo(f"{'updated' if update else 'added'} {pid}")


@app.command(rich_help_panel=BRIEF_PANEL)
def brief(key: str) -> None:
    """Print the brief: rules, facts, open questions and recent changes."""
    from . import ledger

    path = _brief(key, lambda p: p)
    typer.echo(ledger.render(ledger.load(path)), nl=False)


# ---------------------------------------------------------------------------- internal (hooks / tmux)


@app.command(hidden=True)
def hook(event: str) -> None:
    from . import hooks

    hooks.main(event)


@app.command("status-line", hidden=True)
def status_line(
    left: Annotated[str | None, typer.Option("--left")] = None,
    right: Annotated[bool, typer.Option("--right")] = False,
    title: Annotated[str | None, typer.Option("--title")] = None,
) -> None:
    from . import db

    conn = db.connect()
    if title is not None:
        if title == "home":
            typer.echo("wm dashboard")
            return
        ws = db.get_workstream(conn, title)
        typer.echo(ws.label if ws else title)
        return
    if left is not None:
        if left == "home":
            typer.echo("wm · dashboard")
            return
        ws = db.get_workstream(conn, left)
        if ws and db.unseen(conn, ws):  # tmux draws this for each terminal showing the session: you're looking
            db.mark_seen(conn, ws.key, time.time())
        if ws:
            drift = "  ⚠ drift" if ws.drift else ""
            typer.echo(f"{ws.label} · {ws.status.replace('_', ' ')}{drift}")
        else:
            typer.echo(left)
        return
    if right:
        active = db.list_workstreams(conn)
        n_need = sum(ws.status == db.NEEDS_YOU for ws in active)
        n_turn = sum(ws.status == db.YOUR_TURN for ws in active)
        parts = []
        if n_need:
            parts.append(f"#[fg=colour208,bold]⚠ {n_need} need you#[default]")
        if n_turn:
            parts.append(f"◐ {n_turn} your turn")
        typer.echo("  ".join(parts))


@app.command("next", hidden=True)
def next_(
    client: Annotated[str | None, typer.Option("--client")] = None,
    current: Annotated[str | None, typer.Option("--current")] = None,
) -> None:
    """Switch to the workstream that has waited longest for you."""
    from . import db, tmux

    conn = db.connect()
    live = tmux.sessions()
    order = {db.NEEDS_YOU: 0, db.YOUR_TURN: 1}
    candidates = sorted(
        (ws for ws in db.list_workstreams(conn) if ws.status in order and ws.key in live and ws.key != current),
        key=lambda ws: (order[ws.status], ws.status_at),
    )
    if not candidates:
        tmux.run("display-message", *(["-c", client] if client else []), "nothing needs you", check=False)
        return
    tmux.switch_client(candidates[0].key, client)


@app.command(hidden=True)
def dashboard() -> None:
    from .tui import run

    run()


@app.command("app-server", hidden=True)
def app_server() -> None:
    """Backend of the native macOS app: JSON lines on stdin/stdout."""
    from .server import main as serve

    serve()


def main() -> None:
    app(prog_name="wm")  # also when run as `python -m agentmgr`, as the Mac app's wm is


if __name__ == "__main__":
    sys.exit(main())
