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
    from . import gh, view

    svc = _service()
    ws = _run(svc.resolve, svc.db.connect(), ref)
    ws, reports = _run(svc.cleanup_report, ws.key)
    typer.echo(f"{ws.label}:")
    for r in reports:
        state, _ = view.git_state(r.link, r.status, r.pr, after_pr=r.after_pr)
        typer.echo(f"  {r.link.repo:<28} {r.link.branch:<40} {state} · {gh.describe(r.pr)}")
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

    ok = True
    for tool, args, required in (("git", ["--version"], True), ("tmux", ["-V"], True),
                                 ("claude", ["--version"], True), ("gh", ["--version"], False)):
        path = shutil.which(tool)
        if not path:
            ok = ok and not required
            typer.secho(f"{'✗' if required else '!'} {tool}: not found", fg="red" if required else "yellow")
            continue
        out = subprocess.run([tool, *args], capture_output=True, text=True).stdout.splitlines()
        typer.echo(f"✓ {tool}: {out[0] if out else path}")
    from .config import get_config

    cfg = get_config()
    typer.echo(f"  home: {cfg.home}\n  workstreams: {cfg.workstreams_dir}\n  code roots: "
               + ", ".join(map(str, cfg.code_roots)))
    if not _trusted(cfg.workstreams_dir):
        typer.secho(
            f"! {cfg.workstreams_dir} is not trusted by Claude Code yet: every new workstream will show the trust "
            f"dialog. Fix once: mkdir -p {cfg.workstreams_dir} && cd {cfg.workstreams_dir} && claude "
            "(choose 'Yes, I trust this folder', then exit)", fg="yellow")
    if not shutil.which("wm"):
        typer.secho("! `wm` is not on PATH; Claude sessions call it during intake", fg="yellow")
    if not ok:
        raise typer.Exit(1)


def _trusted(folder) -> bool:
    """Read-only check of Claude Code's folder trust (a trusted ancestor covers subfolders)."""
    from pathlib import Path

    try:
        projects = json.loads((Path.home() / ".claude.json").read_text()).get("projects", {})
    except (OSError, ValueError):
        return False
    folder = Path(folder).resolve()
    return any(projects.get(str(p), {}).get("hasTrustDialogAccepted") for p in (folder, *folder.parents))


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


def main() -> None:
    app()


if __name__ == "__main__":
    sys.exit(main())
