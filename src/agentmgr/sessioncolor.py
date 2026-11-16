"""The colour Claude Code's `/color` gives a session's prompt bar, so the app can show it on the workstream's row.

`/color red` makes Claude append `{"type":"agent-color","agentColor":"red",...}` to the session's transcript
(`/color default` writes "default"). `/clear` starts a new transcript with no colour, as Claude's prompt bar then has
none, so the current colour is the last one in the workstream's newest transcript. Nothing outside Claude can set it:
wm types `/color <name>` into the session, and only when Claude waits at an empty prompt (prompt_ready).
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from . import db, naming

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")  # a session's transcript name
COLORS = ("red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan")  # Claude Code 2.1's /color names
MARKER = b'"agent-color"'  # cheap test before parsing a line


@dataclass
class _Scan:
    size: int
    mtime_ns: int
    offset: int  # read up to here: the end of the last complete line
    color: str | None


_cache: dict[Path, _Scan] = {}
_lock = threading.Lock()


def transcripts_dir(ws: db.Workstream) -> Path:
    return Path.home() / ".claude" / "projects" / naming.claude_slug(ws.path)


def current(ws: db.Workstream) -> str | None:
    """The colour of the workstream's Claude session, or None (Claude's default)."""
    try:
        sessions = [p for p in transcripts_dir(ws).glob("*.jsonl") if UUID.fullmatch(p.stem)]
        newest = max(sessions, key=lambda p: p.stat().st_mtime_ns, default=None)
    except OSError:
        return None
    return transcript_color(newest) if newest else None


def transcript_color(path: Path) -> str | None:
    """The last colour set in a transcript. Transcripts only grow, so each call reads just what was appended."""
    try:
        st = path.stat()
    except OSError:
        return None
    with _lock:
        scan = _cache.get(path)
        if scan and (scan.size, scan.mtime_ns) == (st.st_size, st.st_mtime_ns):
            return scan.color
        if scan is None or st.st_size < scan.offset:  # new, or rewritten: read it all
            scan = _Scan(0, 0, 0, None)
        try:
            with path.open("rb") as f:
                f.seek(scan.offset)
                data = f.read()
        except OSError:
            return scan.color
        end = data.rfind(b"\n") + 1  # a line still being written is read next time
        color = scan.color
        for line in data[:end].splitlines():
            if MARKER in line:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("type") == "agent-color":
                    color = entry.get("agentColor") if entry.get("agentColor") in COLORS else None
        if len(_cache) > 256:  # one entry per transcript ever read: keep it bounded
            _cache.clear()
        _cache[path] = _Scan(st.st_size, st.st_mtime_ns, scan.offset + end, color)
        return color


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# a dim run, up to the reset that ends it (ESC[0m, ESC[22m, ESC[m, or a combined one like ESC[0;1m): the prompt's
# placeholder. A dim run with no reset on the line isn't taken as a placeholder, so the prompt counts as not empty.
DIM = re.compile(r"\x1b\[2m.*?\x1b\[(?:0|22)?(?:;[0-9;]*)?m")
PROMPT = "❯"
RULE = "─"


def _plain(line: str) -> str:
    return ANSI.sub("", line).replace("\xa0", " ").strip()


def _rule(plain: str) -> bool:
    return bool(plain) and set(plain) == {RULE}


def prompt_text(screen: str) -> str | None:
    """What is typed at Claude's prompt in `screen` (tmux.capture), its dim placeholder left out; None when no
    one-line prompt shows. The prompt is the last line starting with ❯ between two rules: a draft over several
    lines has no rule right under its first line, so it's never taken for an empty prompt."""
    lines = screen.splitlines()
    plain = [_plain(line) for line in lines]
    for i in range(len(lines) - 2, 0, -1):
        if plain[i].startswith(PROMPT) and _rule(plain[i - 1]):
            if not _rule(plain[i + 1]):
                return None
            return _plain(DIM.sub("", lines[i])).removeprefix(PROMPT).strip()
    return None


def prompt_ready(screen: str, vim: bool) -> bool:
    """Claude waits at an empty prompt in `screen`, where typing a command can't mix with a message you've started.
    With vim keys, only in insert mode (shown as "-- INSERT --"): in normal mode keys are commands."""
    if prompt_text(screen) != "":
        return False
    return not vim or any("-- INSERT --" in _plain(line) for line in screen.splitlines())


def vim_keys() -> bool:
    """Claude Code is set to vim keys (`editorMode`, in its settings or, from older versions, ~/.claude.json). A file
    that can't be read right now (Claude rewrites ~/.claude.json often) counts as vim: the cost is waiting a tick."""
    for path in (Path.home() / ".claude" / "settings.json", Path.home() / ".claude.json"):
        try:
            text = path.read_text()
        except FileNotFoundError:
            continue
        except OSError:
            return True
        try:
            settings = json.loads(text)
        except ValueError:
            return True
        if isinstance(settings, dict) and settings.get("editorMode") == "vim":
            return True
    return False
