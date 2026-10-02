"""SQLite state. Written concurrently by the CLI, hooks and the dashboard."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .config import get_config

SCHEMA = """
CREATE TABLE IF NOT EXISTS workstreams (
    key           TEXT PRIMARY KEY,
    ticket        TEXT,
    name          TEXT,
    session_id    TEXT NOT NULL UNIQUE,
    folder        TEXT NOT NULL,
    status        TEXT NOT NULL,
    status_reason TEXT,
    status_at     REAL NOT NULL,
    created_at    REAL NOT NULL,
    archived_at   REAL,
    drift         TEXT
);
CREATE TABLE IF NOT EXISTS repos (
    ws_key         TEXT NOT NULL REFERENCES workstreams(key),
    repo           TEXT NOT NULL,
    repo_path      TEXT NOT NULL,
    worktree_path  TEXT NOT NULL,
    branch         TEXT NOT NULL,
    base           TEXT NOT NULL,
    created_branch INTEGER NOT NULL,
    added_at       REAL NOT NULL,
    PRIMARY KEY (ws_key, repo)
);
CREATE TABLE IF NOT EXISTS prs (
    ws_key     TEXT NOT NULL,
    repo       TEXT NOT NULL,
    number     INTEGER,
    state      TEXT,
    is_draft   INTEGER,
    review     TEXT,
    checks     TEXT,
    url        TEXT,
    fetched_at REAL NOT NULL,
    mergeable   TEXT,   -- MERGEABLE / CONFLICTING / UNKNOWN
    merge_state TEXT,   -- GitHub mergeStateStatus: CLEAN / BEHIND / BLOCKED / DIRTY / ...
    head_sha    TEXT,   -- PR head commit, to spot local commits made after a merge
    PRIMARY KEY (ws_key, repo)
);
CREATE TABLE IF NOT EXISTS fetches (
    repo_path  TEXT PRIMARY KEY,
    fetched_at REAL NOT NULL,   -- last attempt
    ok_at      REAL,            -- last success
    error      TEXT
);
CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""

# Columns added after the first release: {table: {column: type}}, appended to existing databases on connect.
ADDED_COLUMNS = {"prs": {"mergeable": "TEXT", "merge_state": "TEXT", "head_sha": "TEXT"}}

# Status values
INTAKE = "intake"
WORKING = "working"
NEEDS_YOU = "needs_you"
YOUR_TURN = "your_turn"
STOPPED = "stopped"
ARCHIVED = "archived"


@dataclass
class Workstream:
    key: str
    ticket: str | None
    name: str | None
    session_id: str
    folder: str
    status: str
    status_reason: str | None
    status_at: float
    created_at: float
    archived_at: float | None
    drift: str | None = None

    @property
    def path(self) -> Path:
        return Path(self.folder)

    @property
    def label(self) -> str:
        """Human label: 'PROJ-313 dark mode toggle'."""
        parts = [self.ticket or self.key]
        if self.name:
            parts.append(self.name)
        return " ".join(parts)


@dataclass
class RepoLink:
    ws_key: str
    repo: str
    repo_path: str
    worktree_path: str
    branch: str
    base: str
    created_branch: bool
    added_at: float


@dataclass
class PR:
    ws_key: str
    repo: str
    number: int | None
    state: str | None
    is_draft: bool
    review: str | None
    checks: str | None
    url: str | None
    fetched_at: float
    mergeable: str | None = None
    merge_state: str | None = None
    head_sha: str | None = None


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or get_config().db_path
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _add_columns(conn)
    return conn


def _add_columns(conn: sqlite3.Connection) -> None:
    for table, columns in ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, kind in columns.items():
            if name not in have:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
                except sqlite3.OperationalError as e:  # another process added it first
                    if "duplicate column" not in str(e):
                        raise


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def next_counter(conn: sqlite3.Connection, name: str) -> int:
    with transaction(conn):
        conn.execute(
            "INSERT INTO counters(name, value) VALUES (?, 1) "
            "ON CONFLICT(name) DO UPDATE SET value = value + 1",
            (name,),
        )
        return conn.execute("SELECT value FROM counters WHERE name = ?", (name,)).fetchone()[0]


def _ws(row: sqlite3.Row | None) -> Workstream | None:
    return Workstream(**dict(row)) if row else None


def get_workstream(conn: sqlite3.Connection, key: str) -> Workstream | None:
    return _ws(conn.execute("SELECT * FROM workstreams WHERE key = ?", (key,)).fetchone())


def find_by_session(conn: sqlite3.Connection, session_id: str) -> Workstream | None:
    return _ws(conn.execute("SELECT * FROM workstreams WHERE session_id = ?", (session_id,)).fetchone())


def find_by_folder(conn: sqlite3.Connection, path: str) -> Workstream | None:
    """Workstream whose folder contains `path` (hooks may run from a subfolder)."""
    for ws in list_workstreams(conn, include_archived=False):
        folder = ws.folder.rstrip("/")
        if path == folder or path.startswith(folder + "/"):
            return ws
    return None


def list_workstreams(conn: sqlite3.Connection, include_archived: bool = False) -> list[Workstream]:
    sql = "SELECT * FROM workstreams"
    if not include_archived:
        sql += " WHERE status != 'archived'"
    return [Workstream(**dict(r)) for r in conn.execute(sql + " ORDER BY created_at")]


def insert_workstream(conn: sqlite3.Connection, ws: Workstream) -> None:
    conn.execute(
        "INSERT INTO workstreams VALUES (:key, :ticket, :name, :session_id, :folder, :status, "
        ":status_reason, :status_at, :created_at, :archived_at, :drift)",
        ws.__dict__,
    )


def update_workstream(conn: sqlite3.Connection, key: str, **fields) -> None:
    cols = ", ".join(f"{k} = :{k}" for k in fields)
    conn.execute(f"UPDATE workstreams SET {cols} WHERE key = :key", {**fields, "key": key})


def set_status(conn: sqlite3.Connection, key: str, status: str, reason: str | None = None) -> None:
    update_workstream(conn, key, status=status, status_reason=reason, status_at=time.time())


def list_repos(conn: sqlite3.Connection, ws_key: str) -> list[RepoLink]:
    rows = conn.execute("SELECT * FROM repos WHERE ws_key = ? ORDER BY added_at", (ws_key,))
    return [RepoLink(**{**dict(r), "created_branch": bool(r["created_branch"])}) for r in rows]


def insert_repo(conn: sqlite3.Connection, link: RepoLink) -> None:
    conn.execute(
        "INSERT INTO repos VALUES (:ws_key, :repo, :repo_path, :worktree_path, :branch, :base, "
        ":created_branch, :added_at)",
        {**link.__dict__, "created_branch": int(link.created_branch)},
    )


def delete_repo(conn: sqlite3.Connection, ws_key: str, repo: str) -> None:
    conn.execute("DELETE FROM repos WHERE ws_key = ? AND repo = ?", (ws_key, repo))
    conn.execute("DELETE FROM prs WHERE ws_key = ? AND repo = ?", (ws_key, repo))


def upsert_pr(conn: sqlite3.Connection, pr: PR) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO prs (ws_key, repo, number, state, is_draft, review, checks, url, fetched_at, "
        "mergeable, merge_state, head_sha) VALUES (:ws_key, :repo, :number, :state, :is_draft, :review, "
        ":checks, :url, :fetched_at, :mergeable, :merge_state, :head_sha)",
        {**pr.__dict__, "is_draft": int(pr.is_draft)},
    )


def get_prs(conn: sqlite3.Connection, ws_key: str) -> dict[str, PR]:
    rows = conn.execute("SELECT * FROM prs WHERE ws_key = ?", (ws_key,))
    return {r["repo"]: PR(**{**dict(r), "is_draft": bool(r["is_draft"])}) for r in rows}


def record_fetch(conn: sqlite3.Connection, repo_path: str, error: str | None) -> None:
    now = time.time()
    conn.execute(
        "INSERT INTO fetches(repo_path, fetched_at, ok_at, error) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(repo_path) DO UPDATE SET fetched_at = excluded.fetched_at, error = excluded.error, "
        "ok_at = COALESCE(excluded.ok_at, fetches.ok_at)",
        (repo_path, now, None if error else now, error),
    )


def get_fetches(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["repo_path"]: r for r in conn.execute("SELECT * FROM fetches")}
