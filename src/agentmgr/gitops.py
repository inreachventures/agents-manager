"""Thin git wrapper."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(repo: Path | str, *args: str, check: bool = True, timeout: float | None = None) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {' '.join(args)} timed out after {timeout:.0f}s in {repo}") from e
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def ok(repo: Path | str, *args: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True).returncode == 0


def has_remote(repo: Path, remote: str = "origin") -> bool:
    return remote in git(repo, "remote").split()


def fetch(repo: Path) -> None:
    if has_remote(repo):
        git(repo, "fetch", "--quiet", "--prune", "origin")


def fetch_base(repo: Path, base: str, timeout: float = 20) -> None:
    """Refresh just the base branch (e.g. origin/main) so 'behind base' is accurate. Never merges anything."""
    if base.startswith("origin/") and has_remote(repo):
        branch = base.removeprefix("origin/")
        git(repo, "fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", timeout=timeout)


def default_base(repo: Path) -> str:
    """origin/HEAD target (e.g. origin/main), else a local main/master, else HEAD's branch."""
    if has_remote(repo):
        ref = git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False)
        if ref:
            return ref
        for name in ("main", "master"):
            if ok(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{name}"):
                return f"origin/{name}"
    for name in ("main", "master"):
        if ok(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"):
            return name
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def local_branches(repo: Path) -> list[str]:
    out = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    return [b for b in out.splitlines() if b]


def remote_branches(repo: Path) -> list[str]:
    out = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin")
    return [b.removeprefix("origin/") for b in out.splitlines() if b and b not in ("origin", "origin/HEAD")]


def branch_exists(repo: Path, branch: str) -> bool:
    return ok(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")


def ref_exists(repo: Path, ref: str) -> bool:
    return ok(repo, "rev-parse", "--verify", "--quiet", ref)


@dataclass
class Worktree:
    path: Path
    branch: str | None
    head: str


def worktrees(repo: Path) -> list[Worktree]:
    out = git(repo, "worktree", "list", "--porcelain")
    result: list[Worktree] = []
    cur: dict[str, str] = {}
    for line in out.splitlines() + [""]:
        if not line:
            if cur:
                branch = cur.get("branch", "").removeprefix("refs/heads/") or None
                result.append(Worktree(Path(cur["worktree"]), branch, cur.get("HEAD", "")))
            cur = {}
            continue
        k, _, v = line.partition(" ")
        cur[k] = v
    return result


def checked_out_at(repo: Path, branch: str) -> Path | None:
    for wt in worktrees(repo):
        if wt.branch == branch:
            return wt.path
    return None


def worktree_add(repo: Path, path: Path, branch: str, *, create_from: str | None) -> None:
    """Create a worktree. With `create_from`, a new branch is created from that ref.
    Existing remote-only branches are created locally tracking origin/<branch>."""
    if create_from:
        git(repo, "worktree", "add", "--quiet", "--no-track", "-b", branch, str(path), create_from)
    elif branch_exists(repo, branch):
        git(repo, "worktree", "add", "--quiet", str(path), branch)
    else:
        git(repo, "worktree", "add", "--quiet", "--track", "-b", branch, str(path), f"origin/{branch}")


def worktree_remove(repo: Path, path: Path, *, force: bool = False) -> None:
    args = ["worktree", "remove", str(path)]
    if force:
        args.insert(2, "--force")
    git(repo, *args)


def delete_branch(repo: Path, branch: str, *, force: bool = False) -> None:
    git(repo, "branch", "-D" if force else "-d", branch)


def current_branch(worktree: Path) -> str | None:
    b = git(worktree, "branch", "--show-current", check=False)
    return b or None


@dataclass
class Status:
    branch: str | None
    dirty: int  # changed/untracked files
    ahead_of_base: int  # commits on branch not in base
    behind_base: int
    upstream: str | None
    unpushed: int  # commits not in upstream (== ahead_of_base when there is no upstream)

    @property
    def clean(self) -> bool:
        return self.dirty == 0

    def summary(self) -> str:
        parts = []
        if self.dirty:
            parts.append(f"{self.dirty} uncommitted")
        if self.unpushed:
            parts.append(f"{self.unpushed} unpushed")
        if self.behind_base:
            parts.append(f"{self.behind_base} behind base")
        return " · ".join(parts) or "clean"


def count_commits(worktree: Path, rng: str) -> int:
    """Commits in a range like 'a..b'; 0 if a ref is unknown."""
    out = git(worktree, "rev-list", "--count", rng, check=False)
    return int(out) if out.isdigit() else 0


def status(worktree: Path, base: str) -> Status:
    porcelain = git(worktree, "status", "--porcelain")
    dirty = len([line for line in porcelain.splitlines() if line.strip()])
    branch = current_branch(worktree)
    candidates = [git(worktree, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)]
    if branch:
        candidates.append(f"origin/{branch}")  # pushed without -u
    # the configured upstream can be gone (remote branch deleted after a merge): then nothing counts as pushed
    upstream = next((u for u in candidates if u and ref_exists(worktree, u)), None)
    ahead = count_commits(worktree, f"{base}..HEAD")
    return Status(
        branch=branch,
        dirty=dirty,
        ahead_of_base=ahead,
        behind_base=count_commits(worktree, f"HEAD..{base}"),
        upstream=upstream,
        unpushed=count_commits(worktree, f"{upstream}..HEAD") if upstream else ahead,
    )


def is_merged(repo: Path, branch: str, base: str) -> bool:
    """True if the branch tip is contained in base (regular merges; squash merges are detected via gh)."""
    return ok(repo, "merge-base", "--is-ancestor", branch, base)


def remote_url(repo: Path) -> str | None:
    return git(repo, "remote", "get-url", "origin", check=False) or None


def git_dir(worktree: Path) -> Path:
    return Path(git(worktree, "rev-parse", "--absolute-git-dir"))


def operation_in_progress(worktree: Path) -> str | None:
    """'merge' / 'rebase' / 'cherry-pick' / 'revert' / 'bisect' if one is in progress in this worktree."""
    gd = git_dir(worktree)
    for marker, name in (("MERGE_HEAD", "merge"), ("rebase-merge", "rebase"), ("rebase-apply", "rebase"),
                         ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert"), ("BISECT_LOG", "bisect")):
        if (gd / marker).exists():
            return name
    return None


def rebase(worktree: Path, onto: str) -> list[str]:
    """Rebase the checked-out branch onto `onto`. On conflicts the rebase is aborted, leaving the branch as it was,
    and the conflicting files are returned; [] on success."""
    try:
        git(worktree, "rebase", onto)
        return []
    except GitError:
        conflicts = git(worktree, "diff", "--name-only", "--diff-filter=U", check=False).splitlines()
        if operation_in_progress(worktree) == "rebase":
            git(worktree, "rebase", "--abort")
        if not conflicts:
            raise
        return conflicts


def force_push_with_lease(worktree: Path, branch: str, expected: str, timeout: float = 60) -> None:
    """Push HEAD to origin/<branch>, only if the remote branch is still at `expected` (nobody else pushed)."""
    try:
        git(worktree, "push", "--quiet", f"--force-with-lease=refs/heads/{branch}:{expected}", "origin",
            f"HEAD:refs/heads/{branch}", timeout=timeout)
    except GitError as e:
        if "stale info" in str(e):
            raise GitError(f"someone else pushed to origin/{branch} meanwhile, so it was left as it is") from e
        raise


def is_dirty(worktree: Path) -> bool:
    return bool(git(worktree, "status", "--porcelain"))


def stash_push(worktree: Path, message: str) -> str:
    """Stash tracked + untracked changes; returns the stash commit sha."""
    git(worktree, "stash", "push", "--include-untracked", "--message", message)
    return git(worktree, "rev-parse", "stash@{0}")


def stash_apply(worktree: Path, sha: str) -> None:
    if not ok(worktree, "stash", "apply", "--index", sha):
        git(worktree, "stash", "apply", sha)


def stash_drop(repo: Path, sha: str) -> None:
    """Drop the stash entry with this sha (the stash list is shared by all worktrees of a repo)."""
    for i, entry in enumerate(git(repo, "stash", "list", "--format=%H").splitlines()):
        if entry == sha:
            git(repo, "stash", "drop", "--quiet", f"stash@{{{i}}}")
            return


def is_main_worktree(repo: Path, path: Path) -> bool:
    wts = worktrees(repo)
    return bool(wts) and wts[0].path.resolve() == path.resolve()
