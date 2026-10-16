"""Read-only diffs for the native app: what a workstream branch changes against its base, committed or not."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import gitops

MAX_FILE_LINES = 2000  # diff lines shown per file; the rest of the file is summarised by its counts
MAX_TOTAL_LINES = 8000  # per repo
MAX_UNTRACKED_BYTES = 512_000
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass
class Line:
    kind: str  # "+", "-" or " "
    old: int | None
    new: int | None
    text: str


@dataclass
class Hunk:
    header: str
    lines: list[Line] = field(default_factory=list)


@dataclass
class FileDiff:
    path: str
    old_path: str | None = None
    status: str = "modified"  # added / deleted / modified / renamed
    untracked: bool = False
    binary: bool = False
    additions: int = 0
    deletions: int = 0
    truncated: bool = False  # some lines left out (see MAX_FILE_LINES / MAX_TOTAL_LINES)
    hunks: list[Hunk] = field(default_factory=list)


def _git(worktree: Path, *args: str) -> str:
    """Raw stdout (gitops.git strips it, which would eat a trailing blank context line)."""
    proc = subprocess.run(["git", "-C", str(worktree), "-c", "core.quotePath=false", *args], capture_output=True,
                          text=True, errors="replace", env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if proc.returncode != 0:
        raise gitops.GitError(f"git {' '.join(args)} failed in {worktree}: {proc.stderr.strip()}")
    return proc.stdout


def _unquote(path: str) -> str:
    return path[1:-1] if len(path) > 1 and path[0] == path[-1] == '"' else path


def _header_path(line: str) -> str:
    """Path from `diff --git a/X b/X`; only used when there are no ---/+++ lines (binary, mode-only changes)."""
    rest = line.removeprefix("diff --git ")
    n = (len(rest) - 5) // 2  # "a/" + X + " b/" + X
    return _unquote(rest[2:2 + n]) if n > 0 else rest


def parse(patch: str, budget: int = MAX_TOTAL_LINES) -> list[FileDiff]:
    """Parse `git diff` output into files, hunks and numbered lines."""
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    shown = old = new = 0
    for line in patch.split("\n"):
        if line.startswith("diff --git "):
            cur, hunk, shown = FileDiff(path=_header_path(line)), None, 0
            files.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("@@"):
            m = HUNK_RE.match(line)
            old, new = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
            hunk = Hunk(line)
            if shown < MAX_FILE_LINES and budget > 0:
                cur.hunks.append(hunk)
            continue
        if hunk is None:  # file header
            if line.startswith("new file mode"):
                cur.status = "added"
            elif line.startswith("deleted file mode"):
                cur.status = "deleted"
            elif line.startswith("rename from "):
                cur.old_path, cur.status = _unquote(line.removeprefix("rename from ")), "renamed"
            elif line.startswith("rename to "):
                cur.path = _unquote(line.removeprefix("rename to "))
            elif line.startswith("Binary files ") or line == "GIT binary patch":
                cur.binary = True
            elif line.startswith("+++ ") and line != "+++ /dev/null":
                cur.path = _unquote(line[4:].rstrip("\t")).removeprefix("b/")
            continue
        kind = line[:1]
        if kind not in ("+", "-", " "):
            continue  # "\ No newline at end of file", or the empty string after the last line
        if kind == "+":
            cur.additions += 1
        elif kind == "-":
            cur.deletions += 1
        if shown >= MAX_FILE_LINES or budget <= 0:
            cur.truncated = True
        else:
            hunk.lines.append(Line(kind, None if kind == "+" else old, None if kind == "-" else new, line[1:]))
            shown += 1
            budget -= 1
        if kind != "+":
            old += 1
        if kind != "-":
            new += 1
    return files


def _untracked(worktree: Path) -> list[FileDiff]:
    """New files git doesn't track yet (ignored ones excluded), shown as all-added."""
    files = []
    for name in filter(None, _git(worktree, "ls-files", "--others", "--exclude-standard", "-z").split("\0")):
        path = worktree / name
        f = FileDiff(path=name, status="added", untracked=True)
        files.append(f)
        try:
            if path.is_dir() or path.stat().st_size > MAX_UNTRACKED_BYTES:
                f.truncated = True
                continue
            data = path.read_bytes()
        except OSError:
            f.truncated = True
            continue
        if b"\0" in data[:8000]:
            f.binary = True
            continue
        lines = data.decode("utf-8", errors="replace").splitlines()
        f.additions = len(lines)
        f.hunks = [Hunk(f"@@ -0,0 +1,{len(lines)} @@",
                        [Line("+", None, i + 1, text) for i, text in enumerate(lines[:MAX_FILE_LINES])])]
        f.truncated = len(lines) > MAX_FILE_LINES
    return files


def repo_diff(worktree: Path, base: str) -> dict:
    """Everything the branch changes against where it forked from `base`: commits, staged, unstaged and untracked
    files. Never writes to the repo (no index refresh, no intent-to-add)."""
    fork = _git(worktree, "merge-base", base, "HEAD").strip() if gitops.ref_exists(worktree, base) else "HEAD"
    files = parse(_git(worktree, "--no-optional-locks", "diff", "--no-color", "--no-ext-diff", "-M", fork))
    files += _untracked(worktree)
    return {
        "fork_point": fork[:12],
        "files": [asdict(f) for f in files],
        "additions": sum(f.additions for f in files),
        "deletions": sum(f.deletions for f in files),
    }
