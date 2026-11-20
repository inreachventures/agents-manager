"""Thin git wrapper."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


class GitTimeout(GitError):
    pass


def git(repo: Path | str, *args: str, check: bool = True, timeout: float | None = None) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except subprocess.TimeoutExpired as e:
        raise GitTimeout(f"git {' '.join(args)} timed out after {timeout:.0f}s in {repo}") from e
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
    unpushed: int  # commits on no remote branch and not in base: a push under any name counts

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


def count_outside(worktree: Path, *exclude: str, remotes: bool = False, rev: str = "HEAD") -> int | None:
    """Commits on `rev` reachable from none of `exclude` (refs or shas; unknown ones are skipped), nor from any branch
    on origin if `remotes`. None if git can't tell (a damaged repository), so callers can stay on the safe side."""
    out = git(worktree, "rev-list", "--count", "--ignore-missing", rev, "--not", *exclude,
              *(["--remotes=origin"] if remotes else []), check=False)
    return int(out) if out.isdigit() else None


def _raw(worktree: Path, *args: str, stdin: bytes | None = None) -> bytes:
    """git's stdout as bytes, unstripped: diffs carry file contents in any encoding, and a trailing blank context line
    is part of the patch."""
    proc = subprocess.run(["git", "-C", str(worktree), *args], input=stdin, capture_output=True,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed in {worktree}: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


# Patches compared byte for byte: --verbatim, because patch-id's default drops whitespace, which would make a fix to
# Python indentation "the same change" as the code it fixes. No textconv or external diff, and the settings a user's
# git config could change are pinned (context lines, how submodules show). A binary file is told apart by its full
# blob ids (--full-index), so its content isn't piped through patch-id.
PATCH_FLAGS = ("-p", "--no-color", "--no-ext-diff", "--no-textconv", "--full-index", "-U3", "--submodule=short",
               "--ignore-submodules=none")


def _patch_id_lines(worktree: Path, patch: bytes) -> list[tuple[str, str]]:
    """(patch id, commit sha) per patch in `patch` (all-zero sha for a plain diff)."""
    if not patch:
        return []
    out = _raw(worktree, "patch-id", "--verbatim", stdin=patch).decode("ascii")
    return [(pid, sha) for pid, sha in (line.split() for line in out.splitlines() if line.strip())]


_commit_patch_ids: dict[str, str | None] = {}  # commit sha -> patch id, None for a merge or an empty commit


def _patch_ids(worktree: Path, shas: list[str]) -> dict[str, str | None]:
    """Patch id of each commit: the same change has the same id whatever commit it sits on top of. Kept per sha, as a
    commit's change never changes; the shas go on stdin, however many there are."""
    todo = [sha for sha in dict.fromkeys(shas) if sha not in _commit_patch_ids]
    if todo:
        out = _raw(worktree, "log", *PATCH_FLAGS, "--diff-merges=off", "--no-walk=unsorted", "--stdin",
                   stdin="\n".join(todo).encode())
        found = {sha: pid for pid, sha in _patch_id_lines(worktree, out)}
        _commit_patch_ids.update((sha, found.get(sha)) for sha in todo)
    return {sha: _commit_patch_ids[sha] for sha in shas}


def _diff_id(worktree: Path, *diff_args: str) -> str | None:
    """Patch id of `git diff <diff_args>`; None for an empty diff. Raises GitError if a commit isn't here (a PR head
    made on GitHub and never fetched)."""
    lines = _patch_id_lines(worktree, _raw(worktree, "diff", *PATCH_FLAGS, *diff_args))
    return lines[0][0] if lines else None


_run_diff_ids: dict[tuple[str, str], str | None] = {}  # (from, to) commit shas -> _diff_id


def _run_diff_id(worktree: Path, start: str, end: str) -> str | None:
    if (start, end) not in _run_diff_ids:
        _run_diff_ids[start, end] = _diff_id(worktree, start, end)
    return _run_diff_ids[start, end]


_pr_diff_ids: dict[tuple[str, str, str], str | None] = {}  # (worktree, base, PR head) -> _diff_id of base...head


def _pr_diff_id(worktree: Path, base: str, head: str) -> str | None:
    """Patch id of a PR's whole change (from where it left base); None if empty or the head isn't here. Kept per head:
    a PR's fork point doesn't move until it is merged in."""
    key = (str(worktree), base, head)
    if key not in _pr_diff_ids:
        try:
            _pr_diff_ids[key] = _diff_id(worktree, f"{base}...{head}")
        except GitError:
            return None  # not kept: the head may be fetched later
    return _pr_diff_ids[key]


_absorbed: dict[tuple[str, str, str], bool] = {}  # (start, end, target) -> _absorbs, all commit shas


def _absorbs(worktree: Path, start: str, end: str, target: str) -> bool:
    """`target` already has the change from commit `start` to commit `end`: replaying it onto `target` (a three-way
    merge from `start`) changes nothing. Unlike a patch id this places each change in the file, so an edit to one of
    two identical blocks isn't taken for the same edit to the other. False on a conflict, or if git can't tell (older
    than 2.40, a commit not here)."""
    key = (start, end, target)
    if key not in _absorbed:
        proc = subprocess.run(["git", "-C", str(worktree), "merge-tree", "--write-tree", f"--merge-base={start}",
                               target, end], capture_output=True, text=True,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if proc.returncode not in (0, 1):
            return False  # not kept: git may be able to tell later
        tree = proc.stdout.partition("\n")[0]
        _absorbed[key] = proc.returncode == 0 and tree == git(worktree, "rev-parse", f"{target}^{{tree}}", check=False)
    return _absorbed[key]


def not_landed(worktree: Path, base: str, open_heads: list[str], landed_heads: list[str]) -> int:
    """Commits on HEAD that are in neither `base` nor any PR: work no PR carries. `open_heads`, `landed_heads`: head
    commits of the open PRs and of those merged into base. The usual squash-merge flow keeps this at 0 even after
    rebasing the branch onto base and pushing the next chunk under a new name, because each PR's head is excluded.

    A commit that makes the same change as one of the PRs' commits counts as carried too: the same work pushed for a
    PR from another base. So do commits that, together, make the same change as a whole PR (one squashed locally).
    Same change: the same patch, byte for byte (PATCH_FLAGS), and the PR's commit already has it in the same place
    (_absorbs). Such a commit is in the PR's history on GitHub, even if a later commit of the PR undid it.

    Also 0 when the branch's commits outside base and the PRs, replayed onto base from the parent of the oldest,
    change nothing: their changes reached base another way (a cherry-pick, someone else's PR). Only those commits
    are replayed. Merging the whole branch from its fork point instead would count a commit that undoes part of
    already merged work (say, removing a file a PR added) as in base, since it only puts back the fork point's
    state. For the same reason they are replayed only from a commit base has the changes of (one in base, or a
    merged PR's head), and only if none is carried by an open PR alone. If they aren't a simple line (a merge
    among them), they're counted as they are. Raises GitError if git can't list them."""
    heads = [*open_heads, *landed_heads]
    rows = [line.split() for line in git(worktree, "rev-list", "--parents", "--ignore-missing", "HEAD", "--not", base,
                                         *heads).splitlines()]  # newest first: sha, parents
    if not rows:
        return 0
    uncarried, carried_by_open = len(rows), False
    if heads:
        landed_commits = set(git(worktree, "rev-list", "--ignore-missing", *landed_heads, "--not", base).split()
                             if landed_heads else [])
        by_patch: dict[str, list[str]] = {}
        pr_commits = git(worktree, "rev-list", "--ignore-missing", *heads, "--not", base).split()
        for sha, pid in _patch_ids(worktree, pr_commits).items():
            if pid:
                by_patch.setdefault(pid, []).append(sha)
        own = _patch_ids(worktree, [r[0] for r in rows])
        uncarried = 0
        for sha, *parents in rows:
            matches = [x for x in by_patch.get(own[sha] or "", [])
                       if len(parents) == 1 and _absorbs(worktree, parents[0], sha, x)]
            uncarried += not matches
            carried_by_open |= bool(matches) and not landed_commits.intersection(matches)
    if not uncarried:
        return 0
    linear = all(len(r) == 2 for r in rows) and all(rows[i][1] == rows[i + 1][0] for i in range(len(rows) - 1))
    if not linear:
        return uncarried
    start, end = rows[-1][1], rows[0][0]
    if heads and (whole := _run_diff_id(worktree, start, end)):  # a PR squashed locally: its whole change
        if any(_pr_diff_id(worktree, base, head) == whole and _absorbs(worktree, start, end, head) for head in heads):
            return 0
    base_sha = git(worktree, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}", check=False)
    if not base_sha or carried_by_open or count_outside(worktree, base, *landed_heads, rev=start) != 0:
        return uncarried
    return 0 if _absorbs(worktree, start, end, base_sha) else uncarried


def left_right(worktree: Path, ref: str) -> tuple[int, int] | None:
    """(commits on `ref` not on HEAD, commits on HEAD not on `ref`) in one git call; None if `ref` is unknown."""
    counts = git(worktree, "rev-list", "--left-right", "--count", f"{ref}...HEAD", "--", check=False).split()
    return (int(counts[0]), int(counts[1])) if len(counts) == 2 and all(c.isdigit() for c in counts) else None


def status(worktree: Path, base: str) -> Status:
    """Two git calls in the usual case (the dashboard runs this for every repo every few seconds): `git status`
    gives the branch, the changes and how far HEAD is from an upstream that still exists, one rev-list the rest.
    A third only when HEAD is ahead of that upstream, to see whether those commits are on another remote branch."""
    branch, upstream, unpushed, dirty = None, None, None, 0
    for line in git(worktree, "status", "--porcelain=v2", "--branch").splitlines():
        if line.startswith("# branch.head "):
            head = line.removeprefix("# branch.head ")
            branch = None if head == "(detached)" else head
        elif line.startswith("# branch.upstream "):
            upstream = line.removeprefix("# branch.upstream ")
        elif line.startswith("# branch.ab "):  # only there when the upstream exists
            ahead_of_upstream = line.split()[2].removeprefix("+")
            unpushed = int(ahead_of_upstream) if ahead_of_upstream.isdigit() else None  # "+?": status.aheadBehind
        elif line.strip() and not line.startswith("#"):
            dirty += 1
    if unpushed is None:
        # no upstream, or the configured one is gone (remote branch deleted after a merge), or branch pushed
        # without -u: then origin/<branch>, if there is one, counts as pushed
        candidates = dict.fromkeys(u for u in (upstream, f"origin/{branch}" if branch else None) if u)
        upstream = None
        for candidate in candidates:
            if counts := left_right(worktree, candidate):
                upstream, unpushed = candidate, counts[1]
                break
    behind, ahead = left_right(worktree, base) or (0, 0)
    unpushed = ahead if unpushed is None else unpushed
    if unpushed:  # maybe pushed under another name: the branch re-cut for its next PR, the top of a stack
        on_no_remote = count_outside(worktree, base, remotes=True)
        unpushed = unpushed if on_no_remote is None else min(unpushed, on_no_remote)
    return Status(
        branch=branch,
        dirty=dirty,
        ahead_of_base=ahead,
        behind_base=behind,
        upstream=upstream,
        unpushed=unpushed,
    )


def remote_url(repo: Path) -> str | None:
    return git(repo, "remote", "get-url", "origin", check=False) or None


def git_dir(worktree: Path) -> Path:
    """The worktree's git directory: its .git folder, or where its .git file points (a linked worktree), read
    without starting git; git is asked only for other layouts (a subfolder, say)."""
    dot = Path(worktree).absolute() / ".git"
    if dot.is_dir():
        return dot
    try:
        pointer = dot.read_text()
    except OSError:
        pointer = ""
    if pointer.startswith("gitdir: "):
        return dot.parent / pointer.removeprefix("gitdir: ").strip()  # absolute, or relative to the worktree
    return Path(git(worktree, "rev-parse", "--absolute-git-dir"))


def common_dir(gd: Path) -> Path:
    """Where refs, packed-refs and config live: a linked worktree's git dir names it in its commondir file."""
    try:
        return gd / (gd / "commondir").read_text().strip()  # an absolute path replaces gd
    except OSError:
        return gd


OPERATIONS = (("MERGE_HEAD", "merge"), ("rebase-merge", "rebase"), ("rebase-apply", "rebase"),
              ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert"), ("BISECT_LOG", "bisect"))


def operation_in_progress(worktree: Path) -> str | None:
    """'merge' / 'rebase' / 'cherry-pick' / 'revert' / 'bisect' if one is in progress in this worktree."""
    gd = git_dir(worktree)
    for marker, name in OPERATIONS:
        if (gd / marker).exists():
            return name
    return None


def _stamp(path: Path) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size, st.st_ino


def fingerprint(worktree: Path, refs: list[str]) -> tuple | None:
    """A cheap stamp (no git process) of the files git rewrites when the worktree's HEAD, index, `refs` (e.g.
    "refs/remotes/origin/main"), packed refs, config or in-progress operation change. While it stays the same, so
    does what status() and friends report, except for edits to the worktree's files themselves, which only
    `git status` notices. None if the worktree has no git directory."""
    if not (Path(worktree) / ".git").exists():
        return None
    try:
        gd = git_dir(worktree)
    except GitError:
        return None
    if not gd.is_dir():
        return None
    common = common_dir(gd)
    paths = [gd / "HEAD", gd / "index", gd / "logs" / "HEAD", *(gd / marker for marker, _ in OPERATIONS),
             common / "packed-refs", common / "config", common / "reftable" / "tables.list",
             # a branch pushed or fetched under a new name (Status.unpushed); a later push to a nested name (a/b) only
             # touches its subfolder, and shows within the dashboards' max_age
             common / "refs" / "remotes" / "origin",
             *(common / ref for ref in refs)]
    return tuple(_stamp(p) for p in paths)


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
