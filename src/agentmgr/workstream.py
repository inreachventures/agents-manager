"""Workstream service: the only place that creates folders, worktrees, branches and sessions."""

from __future__ import annotations

import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import db, gitops, naming, repos, templates, tmux
from .config import Config, get_config


class WorkstreamError(RuntimeError):
    pass


# ---------------------------------------------------------------------------- helpers


def _conn():
    return db.connect()


def require(conn, key: str) -> db.Workstream:
    ws = db.get_workstream(conn, key.upper()) or db.get_workstream(conn, key)
    if not ws:
        raise WorkstreamError(f"no workstream {key!r}")
    if ws.status == db.ARCHIVED:
        raise WorkstreamError(f"workstream {ws.key} is archived")
    return ws


def resolve(conn, ref: str, archived: bool = False) -> db.Workstream:
    """Find an active (or, with `archived`, an archived) workstream by key, ticket or (partial) name."""
    ref_l = ref.lower()
    kind = "archived" if archived else "active"
    pool = [ws for ws in db.list_workstreams(conn, include_archived=archived)
            if (ws.status == db.ARCHIVED) == archived]
    for ws in pool:
        if ref_l in (ws.key.lower(), (ws.ticket or "").lower()):
            return ws
    hits = [ws for ws in pool if ws.name and ref_l in ws.name]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise WorkstreamError(f"{ref!r} is ambiguous: {', '.join(w.label for w in hits)}")
    raise WorkstreamError(f"no {kind} workstream matches {ref!r}")


def regenerate(conn, ws: db.Workstream, cfg: Config | None = None, pending_repos: list[str] | None = None):
    cfg = cfg or get_config()
    ws = db.get_workstream(conn, ws.key) or ws
    templates.write(
        ws,
        db.list_repos(conn, ws.key),
        [r.name for r in repos.discover(cfg)],
        tmux.wm_executable(),
        [str(r) for r in cfg.code_roots],
        pending_repos,
    )
    _update_symlink(ws, cfg)


def _update_symlink(ws: db.Workstream, cfg: Config) -> None:
    # remove stale links pointing at this folder, then create the current one
    for p in cfg.workstreams_dir.iterdir():
        if p.is_symlink() and p.resolve() == ws.path.resolve():
            p.unlink()
    link = naming.symlink_name(ws.key, ws.ticket, ws.name)
    if link:
        target = cfg.workstreams_dir / link
        if not target.exists():
            target.symlink_to(ws.path.name)


def is_intake(ws: db.Workstream, repo_count: int) -> bool:
    return not (ws.name and repo_count and (ws.path / templates.TASK_FILE).exists())


# ---------------------------------------------------------------------------- new


def new(ticket: str | None = None, name: str | None = None, repo_queries: list[str] | None = None,
        task: str | None = None, launch: bool = True) -> db.Workstream:
    cfg = get_config()
    conn = _conn()
    cfg.workstreams_dir.mkdir(parents=True, exist_ok=True)
    ticket = naming.normalize_ticket(ticket) if ticket else None
    name = naming.normalize_name(name) if name else None

    # validate repos up front so a typo fails before anything is created
    available = repos.discover(cfg)
    matched = [repos.match(q, available) for q in (repo_queries or [])]

    if ticket:
        _ensure_ticket_free(conn, ticket)
        key = ticket
        if db.get_workstream(conn, key):
            raise WorkstreamError(f"{key} was used by an archived workstream; pick another key")
    else:
        key = naming.auto_key(db.next_counter(conn, "ws"))
        while db.get_workstream(conn, key) or (cfg.workstreams_dir / key).exists():
            key = naming.auto_key(db.next_counter(conn, "ws"))

    folder = cfg.workstreams_dir / key
    if folder.exists():
        raise WorkstreamError(f"{folder} already exists")

    now = time.time()
    ws = db.Workstream(
        key=key, ticket=ticket, name=name, session_id=str(uuid.uuid4()), folder=str(folder),
        status=db.INTAKE, status_reason=None, status_at=now, created_at=now, archived_at=None,
    )
    folder.mkdir(parents=True)
    try:
        db.insert_workstream(conn, ws)
        pending = [r.name for r in matched]
        if name:
            for r in matched:
                add_repo(key, r.name, conn=conn, regenerate_files=False)
            pending = []
        regenerate(conn, ws, cfg, pending_repos=pending)
        if launch:
            start_session(ws, initial_prompt=task)
    except BaseException:
        _rollback_new(conn, ws)
        raise
    return db.get_workstream(conn, key)


def _ensure_ticket_free(conn, ticket: str) -> None:
    for ws in db.list_workstreams(conn):
        if ws.ticket == ticket or ws.key == ticket:
            raise WorkstreamError(f"ticket {ticket} is already used by active workstream {ws.key}")


def _rollback_new(conn, ws: db.Workstream) -> None:
    for link in db.list_repos(conn, ws.key):
        _remove_worktree(link, force=True, delete_branch=link.created_branch)
    conn.execute("DELETE FROM repos WHERE ws_key = ?", (ws.key,))
    conn.execute("DELETE FROM workstreams WHERE key = ?", (ws.key,))
    tmux.kill_session(ws.key)
    cfg = get_config()
    for p in cfg.workstreams_dir.iterdir():
        if p.is_symlink() and p.resolve() == ws.path.resolve():
            p.unlink()
    shutil.rmtree(ws.path, ignore_errors=True)


# ---------------------------------------------------------------------------- set


def set_(key: str, name: str | None = None, ticket: str | None = None) -> tuple[db.Workstream, list[str]]:
    """Set name and/or ticket. Returns (workstream, warnings)."""
    conn = _conn()
    ws = require(conn, key)
    fields: dict = {}
    warnings: list[str] = []
    if name is not None:
        fields["name"] = naming.normalize_name(name)
    if ticket is not None:
        t = naming.normalize_ticket(ticket)
        if t != ws.ticket:
            _ensure_ticket_free(conn, t)
        fields["ticket"] = t
    if not fields:
        raise WorkstreamError("nothing to set: pass --name and/or --ticket")
    links = db.list_repos(conn, ws.key)
    if links:
        warnings.append(
            "branches already created keep their names: " + ", ".join(f"{r.repo}:{r.branch}" for r in links)
        )
    db.update_workstream(conn, ws.key, **fields)
    ws = db.get_workstream(conn, ws.key)
    regenerate(conn, ws)
    return ws, warnings


# ---------------------------------------------------------------------------- repos


@dataclass
class AddResult:
    link: db.RepoLink
    created_branch: bool
    notes: list[str] = field(default_factory=list)


def add_repo(key: str, query: str, branch: str | None = None, allow_foreign_branch: bool = False,
             conn=None, regenerate_files: bool = True) -> AddResult:
    cfg = get_config()
    conn = conn or _conn()
    ws = require(conn, key)
    if not ws.name:
        raise WorkstreamError(
            f"workstream {ws.key} has no name yet; run `wm set {ws.key} --name \"<3 words>\"` first "
            "(the branch name is derived from it)"
        )
    repo = repos.match(query, repos.discover(cfg))
    if any(r.repo == repo.name for r in db.list_repos(conn, ws.key)):
        raise WorkstreamError(f"{repo.name} is already attached to {ws.key}")

    notes: list[str] = []
    try:
        gitops.fetch(repo.path)
    except gitops.GitError as e:
        notes.append(f"fetch failed, using local refs: {e}")
    base = gitops.default_base(repo.path)

    if branch:
        if not allow_foreign_branch and not naming.branch_carries_id(branch, ws.key, ws.ticket):
            raise WorkstreamError(
                f"branch {branch!r} does not contain {ws.ticket or ws.key}; "
                "pass --allow-foreign-branch if this is intended"
            )
        exists_local = gitops.branch_exists(repo.path, branch)
        if not exists_local and branch not in gitops.remote_branches(repo.path):
            raise WorkstreamError(f"branch {branch!r} does not exist in {repo.name} (local or origin)")
        create_from = None
        created = False
    else:
        branch = naming.branch_name(ws.key, ws.ticket, ws.name)
        if gitops.branch_exists(repo.path, branch):
            create_from, created = None, False
            notes.append(f"reusing existing local branch {branch}")
        elif branch in gitops.remote_branches(repo.path):
            create_from, created = None, False
            notes.append(f"tracking existing origin/{branch}")
        else:
            create_from, created = base, True

    if where := gitops.checked_out_at(repo.path, branch):
        raise WorkstreamError(
            f"branch {branch} is already checked out at {where}; switch that checkout to another branch first"
        )

    wt_path = ws.path / repo.name
    if wt_path.exists():
        raise WorkstreamError(f"{wt_path} already exists")
    gitops.worktree_add(repo.path, wt_path, branch, create_from=create_from)

    link = db.RepoLink(
        ws_key=ws.key, repo=repo.name, repo_path=str(repo.path), worktree_path=str(wt_path),
        branch=branch, base=base, created_branch=created, added_at=time.time(),
    )
    try:
        db.insert_repo(conn, link)
        if regenerate_files:
            regenerate(conn, ws, cfg)
    except BaseException:
        _remove_worktree(link, force=True, delete_branch=created)
        db.delete_repo(conn, ws.key, repo.name)
        raise
    return AddResult(link, created, notes)


@dataclass
class AdoptPlan:
    repo: repos.Repo
    source: Path  # checkout the work currently lives in
    source_is_main: bool
    branch: str  # branch the worktree will be on
    take_branch: bool  # True: move the source's branch; False: create `branch` from the source's HEAD
    free_to: str | None  # what the source switches to afterwards (default branch, or None = detached HEAD)
    dirty: bool
    base: str

    def describe(self) -> list[str]:
        where = "main checkout" if self.source_is_main else "worktree"
        lines = [f"{self.repo.name}: adopt work from the {where} {self.source}"]
        if self.dirty:
            lines.append("  - stash uncommitted + untracked changes there (ignored files like .env stay behind)")
        if self.take_branch:
            to = f"switch it to {self.free_to}" if self.free_to else "detach its HEAD"
            lines.append(f"  - free branch {self.branch} there ({to})")
        else:
            lines.append(f"  - create branch {self.branch} from its current commit")
        lines.append(f"  - create the worktree on {self.branch}" + (" and apply the changes" if self.dirty else ""))
        return lines


def plan_adopt(key: str, query: str, source: str | None = None, allow_foreign_branch: bool = True,
               conn=None) -> AdoptPlan:
    """Work out how to move in-progress work (a branch and/or uncommitted changes) into the workstream."""
    cfg = get_config()
    conn = conn or _conn()
    ws = require(conn, key)
    if not ws.name:
        raise WorkstreamError(f"workstream {ws.key} has no name yet; run `wm set {ws.key} --name ...` first")
    repo = repos.match(query, repos.discover(cfg))
    if any(r.repo == repo.name for r in db.list_repos(conn, ws.key)):
        raise WorkstreamError(f"{repo.name} is already attached to {ws.key}")
    src = Path(source).expanduser().resolve() if source else repo.path
    known = {wt.path.resolve() for wt in gitops.worktrees(repo.path)}
    if src not in known:
        raise WorkstreamError(f"{src} is not a checkout of {repo.name}")
    if op := gitops.operation_in_progress(src):
        raise WorkstreamError(f"a {op} is in progress in {src}; finish or abort it first")
    branch = gitops.current_branch(src)
    if branch is None:
        raise WorkstreamError(f"{src} is on a detached HEAD; check out the branch with your work first")
    for other in db.list_workstreams(conn):
        if any(r.repo == repo.name and r.branch == branch for r in db.list_repos(conn, other.key)):
            raise WorkstreamError(f"branch {branch} is already attached to workstream {other.key}")

    try:
        gitops.fetch(repo.path)
    except gitops.GitError:
        pass
    base = gitops.default_base(repo.path)
    default_local = base.removeprefix("origin/")
    dirty = gitops.is_dirty(src)
    is_main = gitops.is_main_worktree(repo.path, src)

    if branch == default_local:
        if not dirty:
            raise WorkstreamError(
                f"{src} is on {branch} with no uncommitted changes: nothing to adopt; use `wm add-repo {ws.key} "
                f"{repo.name}`"
            )
        new_branch = naming.branch_name(ws.key, ws.ticket, ws.name)
        if gitops.branch_exists(repo.path, new_branch):
            raise WorkstreamError(f"branch {new_branch} already exists in {repo.name}")
        return AdoptPlan(repo, src, is_main, new_branch, False, None, dirty, base)

    if not allow_foreign_branch and not naming.branch_carries_id(branch, ws.key, ws.ticket):
        raise WorkstreamError(f"branch {branch!r} does not contain {ws.ticket or ws.key}")
    free_to = default_local if is_main else None
    if free_to and (where := gitops.checked_out_at(repo.path, free_to)):
        raise WorkstreamError(f"can't switch {src} to {free_to}: it is checked out at {where}")
    return AdoptPlan(repo, src, is_main, branch, True, free_to, dirty, base)


def adopt(key: str, plan: AdoptPlan, conn=None, regenerate_files: bool = True) -> AddResult:
    """Execute an AdoptPlan. On failure the source checkout is put back as it was."""
    conn = conn or _conn()
    ws = require(conn, key)
    src, repo_path = plan.source, plan.repo.path
    wt_path = ws.path / plan.repo.name
    if wt_path.exists():
        raise WorkstreamError(f"{wt_path} already exists")

    stash = gitops.stash_push(src, f"wm adopt {ws.key}") if plan.dirty else None
    freed = worktree_made = False
    try:
        if plan.take_branch:
            if plan.free_to:
                gitops.git(src, "switch", "--quiet", plan.free_to)
            else:
                gitops.git(src, "switch", "--quiet", "--detach")
            freed = True
            gitops.worktree_add(repo_path, wt_path, plan.branch, create_from=None)
        else:
            head = gitops.git(src, "rev-parse", "HEAD")
            gitops.worktree_add(repo_path, wt_path, plan.branch, create_from=head)
        worktree_made = True
        if stash:
            gitops.stash_apply(wt_path, stash)
    except BaseException:
        if worktree_made:
            gitops.worktree_remove(repo_path, wt_path, force=True)
            if not plan.take_branch:
                gitops.delete_branch(repo_path, plan.branch, force=True)
        if freed:
            gitops.git(src, "switch", "--quiet", plan.branch, check=False)
        if stash:
            gitops.git(src, "stash", "apply", "--index", stash, check=False)
            gitops.stash_drop(repo_path, stash)
        raise
    if stash:
        gitops.stash_drop(repo_path, stash)

    link = db.RepoLink(
        ws_key=ws.key, repo=plan.repo.name, repo_path=str(repo_path), worktree_path=str(wt_path),
        branch=plan.branch, base=plan.base, created_branch=not plan.take_branch, added_at=time.time(),
    )
    db.insert_repo(conn, link)
    if regenerate_files:
        regenerate(conn, ws)
    notes = [f"adopted from {src}" + (" including uncommitted changes" if plan.dirty else "")]
    if plan.take_branch:
        notes.append(f"{src} is now " + (f"on {plan.free_to}" if plan.free_to else "on a detached HEAD"))
    return AddResult(link, not plan.take_branch, notes)


def _remove_worktree(link: db.RepoLink, force: bool, delete_branch: bool) -> None:
    repo_path = Path(link.repo_path)
    if Path(link.worktree_path).exists():
        gitops.worktree_remove(repo_path, Path(link.worktree_path), force=force)
    else:
        gitops.git(repo_path, "worktree", "prune", check=False)
    if delete_branch and gitops.branch_exists(repo_path, link.branch):
        gitops.delete_branch(repo_path, link.branch, force=True)


def remove_repo(key: str, repo_name: str, force: bool = False) -> None:
    """Detach a repo. Without force, only when the worktree is clean and has no commits of its own."""
    conn = _conn()
    ws = require(conn, key)
    link = next((r for r in db.list_repos(conn, ws.key) if r.repo == repo_name), None)
    if not link:
        attached = ", ".join(r.repo for r in db.list_repos(conn, ws.key)) or "none"
        raise WorkstreamError(f"{repo_name} is not attached to {ws.key} (attached: {attached})")
    st = gitops.status(Path(link.worktree_path), link.base)
    if not force and (st.dirty or st.ahead_of_base):
        raise WorkstreamError(
            f"{repo_name} has {st.summary()}; refusing to remove (use --force to discard)"
        )
    _remove_worktree(link, force=force, delete_branch=link.created_branch and st.ahead_of_base == 0)
    db.delete_repo(conn, ws.key, repo_name)
    regenerate(conn, ws)


# ---------------------------------------------------------------------------- sessions


def claude_command(ws: db.Workstream, *, resume: bool, initial_prompt: str | None = None) -> list[str]:
    cfg = get_config()
    cmd = [cfg.claude_cmd]
    cmd += ["--resume", ws.session_id] if resume else ["--session-id", ws.session_id]
    cmd += ["--name", ws.label]
    cmd += ["--allowedTools", ",".join(templates.allowed_tools(ws))]
    if initial_prompt:
        cmd.append(initial_prompt)
    return cmd


def start_session(ws: db.Workstream, initial_prompt: str | None = None, resume: bool = False) -> None:
    if not tmux.available():
        raise WorkstreamError("tmux is not installed (brew install tmux)")
    if tmux.has_session(ws.key):
        return
    tmux.new_session(
        ws.key, ws.path, claude_command(ws, resume=resume, initial_prompt=initial_prompt),
        env={"AGENTMGR_WS": ws.key},
    )


def session_alive(ws: db.Workstream) -> bool:
    return tmux.has_session(ws.key)


def transcript_exists(ws: db.Workstream) -> bool:
    import re

    encoded = re.sub(r"[^A-Za-z0-9]", "-", str(ws.path))
    return (Path.home() / ".claude" / "projects" / encoded / f"{ws.session_id}.jsonl").exists()


def resume(key: str, initial_prompt: str | None = None) -> db.Workstream:
    conn = _conn()
    ws = require(conn, key)
    if session_alive(ws):
        return ws
    # a session that never got a first message has no transcript; start it fresh with the same id
    start_session(ws, initial_prompt=initial_prompt, resume=transcript_exists(ws))
    db.set_status(conn, ws.key, db.YOUR_TURN, "resumed")
    return db.get_workstream(conn, ws.key)


def open_(key: str) -> None:
    conn = _conn()
    ws = require(conn, key)
    if not session_alive(ws):
        resume(ws.key)
    tmux.open_session(ws.key)


# ---------------------------------------------------------------------------- rebase


@dataclass
class RebaseStep:
    link: db.RepoLink
    behind: int = 0
    skip: str | None = None  # why this repo is left alone
    pushed_at: str | None = None  # origin/<branch> before the rebase; force-pushed with this as the lease


def rebase_plan(key: str, fetch: bool = True) -> tuple[db.Workstream, list[RebaseStep]]:
    """What `rebase` would do in each repo of the workstream."""
    conn = _conn()
    ws = require(conn, key)
    if ws.status in (db.WORKING, db.NEEDS_YOU) and session_alive(ws):
        raise WorkstreamError(f"Claude is in the middle of a task in {ws.key}; rebase when it's your turn")
    prs = db.get_prs(conn, ws.key)
    steps = []
    for link in db.list_repos(conn, ws.key):
        wt, remote = Path(link.worktree_path), f"origin/{link.branch}"
        if not wt.exists():
            steps.append(RebaseStep(link, skip="worktree missing"))
            continue
        if fetch:
            try:
                gitops.fetch(Path(link.repo_path))  # the base, and the branch itself so the push lease is current
            except gitops.GitError:
                pass  # go on with what we have: a stale lease makes the push fail rather than overwrite
        st = gitops.status(wt, link.base)
        pr = prs.get(link.repo)
        pushed = gitops.ref_exists(wt, remote)
        operation = gitops.operation_in_progress(wt)
        not_pulled = gitops.count_commits(wt, f"HEAD..{remote}") if pushed else 0
        if pr and pr.state == "merged":
            skip = "PR already merged"
        elif st.branch != link.branch:
            skip = f"on {st.branch or 'detached HEAD'} instead of {link.branch}"
        elif operation:
            skip = f"{operation} in progress"
        elif not st.behind_base:
            skip = f"up to date with {link.base}"
        elif st.dirty:
            skip = f"{st.dirty} uncommitted change(s)"
        elif not_pulled:
            skip = f"{not_pulled} commit(s) on {remote} that aren't local"
        else:
            skip = None
        pushed_at = gitops.git(wt, "rev-parse", remote) if pushed and not skip else None
        steps.append(RebaseStep(link, st.behind_base, skip, pushed_at))
    return ws, steps


def rebase(key: str) -> list[str]:
    """Rebase each repo that is behind its base onto it, then force-push (with lease) the branches already on
    origin. A rebase that conflicts is aborted and handed to the Claude session. Returns log lines."""
    ws, steps = rebase_plan(key, fetch=False)  # the plan shown for confirmation already fetched
    log, conflicts = [], []
    for step in steps:
        if step.skip:
            continue
        link, wt = step.link, Path(step.link.worktree_path)
        try:
            files = gitops.rebase(wt, link.base)
        except gitops.GitError as e:
            log.append(f"{link.repo}: rebase failed, nothing changed ({e})")
            continue
        if files:
            conflicts.append((link, files))
            log.append(f"{link.repo}: conflicts in {', '.join(files)}; rebase aborted, nothing changed")
            continue
        line = f"{link.repo}: rebased onto {link.base}"
        if step.pushed_at:
            try:
                gitops.force_push_with_lease(wt, link.branch, step.pushed_at)
                line += ", force-pushed"
            except gitops.GitError as e:
                line += f", but not pushed: {e}"
        log.append(line)
    if conflicts:
        log.append(_ask_claude_to_rebase(ws, conflicts))
    return log


def _ask_claude_to_rebase(ws: db.Workstream, conflicts: list[tuple[db.RepoLink, list[str]]]) -> str:
    where = "; ".join(f"{Path(link.worktree_path).name}/ onto {link.base} ({', '.join(files)})"
                      for link, files in conflicts)
    prompt = (f"[wm] Rebasing hit conflicts, so wm aborted it and nothing changed: {where}. Please rebase these "
              "branches, resolve the conflicts, run the tests, then push (--force-with-lease if already on origin).")
    if session_alive(ws):
        tmux.send_text(ws.key, prompt)
        return "asked Claude to resolve the conflicts"
    resume(ws.key, initial_prompt=prompt)
    return "resumed the session and asked Claude to resolve the conflicts"


# ---------------------------------------------------------------------------- archive


@dataclass
class RepoReport:
    link: db.RepoLink
    status: gitops.Status | None
    merged: bool
    pr: db.PR | None
    blockers: list[str]
    after_pr: int = 0  # local commits made after the merged PR's head


def cleanup_report(key: str, refresh_prs: bool = True) -> tuple[db.Workstream, list[RepoReport]]:
    from . import gh

    conn = _conn()
    ws = require(conn, key)
    if refresh_prs:
        gh.refresh(conn, ws.key)
    prs = db.get_prs(conn, ws.key)
    reports = []
    for link in db.list_repos(conn, ws.key):
        wt = Path(link.worktree_path)
        pr = prs.get(link.repo)
        if not wt.exists():
            reports.append(RepoReport(link, None, False, pr, []))
            continue
        try:
            gitops.fetch(Path(link.repo_path))
        except gitops.GitError:
            pass
        st = gitops.status(wt, link.base)
        merged = (pr is not None and pr.state == "merged") or (
            st.ahead_of_base > 0 and gitops.is_merged(Path(link.repo_path), link.branch, link.base)
        )
        after_pr = gitops.count_commits(wt, f"{pr.head_sha}..HEAD") if merged and pr and pr.head_sha else 0
        blockers = []
        if st.dirty:
            blockers.append(f"{st.dirty} uncommitted change(s)")
        if st.unpushed and not merged:
            blockers.append(f"{st.unpushed} unpushed commit(s)")
        if after_pr:
            blockers.append(f"{after_pr} commit(s) made after PR #{pr.number} was merged")
        reports.append(RepoReport(link, st, merged, pr, blockers, after_pr))
    return ws, reports


def archive(key: str, force: bool = False, delete_branches: bool = True) -> list[str]:
    """Stop the session, remove worktrees, delete safe-to-delete branches. Returns log lines."""
    conn = _conn()
    ws, reports = cleanup_report(key)
    blocked = [(r.link.repo, b) for r in reports for b in r.blockers]
    if blocked and not force:
        detail = "; ".join(f"{repo}: {b}" for repo, b in blocked)
        raise WorkstreamError(f"refusing to archive {ws.key}: {detail} (use --force to discard)")

    log = []
    tmux.kill_session(ws.key)
    for r in reports:
        no_own_commits = r.status is not None and r.status.ahead_of_base == 0
        delete = delete_branches and (r.merged or (r.link.created_branch and no_own_commits))
        _remove_worktree(r.link, force=force, delete_branch=delete)
        log.append(f"{r.link.repo}: worktree removed" + (f", branch {r.link.branch} deleted" if delete else
                                                          f", branch {r.link.branch} kept"))
    cfg = get_config()
    for p in cfg.workstreams_dir.iterdir():
        if p.is_symlink() and p.resolve() == ws.path.resolve():
            p.unlink()
    db.update_workstream(conn, ws.key, status=db.ARCHIVED, archived_at=time.time(), drift=None)
    log.append(f"{ws.key} archived; TASK.md and the conversation are kept in {ws.path}")
    return log


def unarchive(key: str) -> list[str]:
    """Bring an archived workstream back: recreate its worktrees on the same branches. Returns log lines.

    A branch that was deleted is taken from origin if it is still there, else recreated from the base branch.
    Nothing is changed unless every repo can be restored. The session is left stopped; `open` resumes it with
    the same conversation.
    """
    conn = _conn()
    ws = db.get_workstream(conn, key)
    if not ws or ws.status != db.ARCHIVED:
        raise WorkstreamError(f"{key} is not an archived workstream")
    if ws.ticket:
        _ensure_ticket_free(conn, ws.ticket)

    # plan every repo first, so a conflict fails before anything is created
    plans: list[tuple[db.RepoLink, str | None, str]] = []  # (link, create_from, log line)
    for link in db.list_repos(conn, ws.key):
        repo_path, wt_path = Path(link.repo_path), Path(link.worktree_path)
        if not (repo_path / ".git").exists():
            raise WorkstreamError(f"{link.repo}: {repo_path} is no longer a git checkout")
        if wt_path.exists():
            raise WorkstreamError(f"{link.repo}: {wt_path} already exists")
        try:
            gitops.fetch(repo_path)
        except gitops.GitError:
            pass  # restore from local refs
        if gitops.branch_exists(repo_path, link.branch):
            if where := gitops.checked_out_at(repo_path, link.branch):
                raise WorkstreamError(f"{link.repo}: branch {link.branch} is checked out at {where}")
            plans.append((link, None, f"{link.repo}: worktree restored on {link.branch}"))
        elif link.branch in gitops.remote_branches(repo_path):
            plans.append((link, None, f"{link.repo}: worktree restored on {link.branch} (from origin)"))
        else:
            base = gitops.default_base(repo_path)
            plans.append((link, base, f"{link.repo}: {link.branch} was deleted; recreated from {base}"))

    made: list[tuple[db.RepoLink, bool]] = []
    try:
        ws.path.mkdir(parents=True, exist_ok=True)
        for link, create_from, _ in plans:
            gitops.worktree_add(Path(link.repo_path), Path(link.worktree_path), link.branch,
                                create_from=create_from)
            made.append((link, create_from is not None))
    except BaseException:
        for link, created in made:
            _remove_worktree(link, force=True, delete_branch=created)
        raise

    for link, create_from, _ in plans:
        if create_from:  # a fresh branch: new base, and the workstream owns it again
            db.delete_repo(conn, ws.key, link.repo)
            db.insert_repo(conn, db.RepoLink(**{**link.__dict__, "base": create_from, "created_branch": True}))
    db.update_workstream(conn, ws.key, archived_at=None, drift=None)
    db.set_status(conn, ws.key, db.STOPPED, "unarchived")
    regenerate(conn, db.get_workstream(conn, ws.key))
    return [line for _, _, line in plans] + [f"{ws.key} restored; open it to resume the conversation"]
