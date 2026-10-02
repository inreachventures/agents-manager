"""Keeps each attached repo's base branch (origin/main) fresh, so "behind base" reflects reality.

Only fetches; never merges or rebases workstream branches.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db, gitops

DASHBOARD_MAX_AGE = 300  # seconds between background fetches of the same repo
SESSION_START_MAX_AGE = 60
BEHIND_WARN = 20  # highlight a branch this many commits behind its base


def _targets(conn, ws_key: str | None) -> dict[str, str]:
    """repo_path -> base, for the active workstreams (or one workstream)."""
    keys = [ws_key] if ws_key else [ws.key for ws in db.list_workstreams(conn)]
    targets: dict[str, str] = {}
    for key in keys:
        for link in db.list_repos(conn, key):
            targets.setdefault(link.repo_path, link.base)
    return targets


def fetch_stale(max_age: float, timeout: float = 20, ws_key: str | None = None) -> dict[str, str | None]:
    """Fetch the base of every repo not fetched in the last `max_age` seconds, in parallel.
    Returns repo_path -> error (None on success) for the repos it fetched."""
    conn = db.connect()
    last = db.get_fetches(conn)
    now = time.time()
    due = {p: b for p, b in _targets(conn, ws_key).items()
           if p not in last or now - last[p]["fetched_at"] >= max_age}
    if not due:
        return {}

    def one(item: tuple[str, str]) -> tuple[str, str | None]:
        path, base = item
        try:
            gitops.fetch_base(Path(path), base, timeout=timeout)
            return path, None
        except gitops.GitError as e:
            return path, str(e)

    with ThreadPoolExecutor(max_workers=min(8, len(due))) as pool:
        results = dict(pool.map(one, due.items()))
    for path, error in results.items():
        db.record_fetch(conn, path, error)
    return results
