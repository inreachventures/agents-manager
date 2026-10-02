"""Repo discovery under the code roots, and fuzzy matching of (often voice-transcribed) names."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz, process

from .config import Config


@dataclass(frozen=True)
class Repo:
    name: str
    path: Path


class RepoMatchError(LookupError):
    pass


def discover(cfg: Config) -> list[Repo]:
    """Git repos directly under each code root (main checkouts only, not worktrees)."""
    found: dict[str, Repo] = {}
    for root in cfg.code_roots:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if child.name.startswith(".") or not (child / ".git").is_dir():
                continue
            found.setdefault(child.name, Repo(child.name, child))
    return sorted(found.values(), key=lambda r: r.name)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def match(query: str, repos: list[Repo]) -> Repo:
    """Resolve a repo name. Exact → punctuation/space-insensitive → fuzzy with an ambiguity check."""
    if not repos:
        raise RepoMatchError("no git repositories found under the configured code roots")
    by_name = {r.name: r for r in repos}
    if query in by_name:
        return by_name[query]
    q = _norm(query)
    normalized = [r for r in repos if _norm(r.name) == q]
    if len(normalized) == 1:
        return normalized[0]
    contains = [r for r in repos if q and q in _norm(r.name)]
    if len(contains) == 1:
        return contains[0]

    scored = process.extract(q, {r.name: _norm(r.name) for r in repos}, scorer=fuzz.WRatio, limit=5)
    candidates = [name for _, score, name in scored if score >= 60]
    if scored and scored[0][1] >= 85 and (len(scored) == 1 or scored[0][1] - scored[1][1] >= 10):
        return by_name[scored[0][2]]
    hint = ", ".join(candidates or [r.name for r in repos][:10])
    raise RepoMatchError(f"could not uniquely match repo {query!r}. Candidates: {hint}")
