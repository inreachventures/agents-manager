"""Claude Code's folder trust, kept in ~/.claude.json (a trusted ancestor covers its subfolders).

~/.claude.json is Claude Code's live state file and running sessions rewrite it, so we only ever
touch it when the folder is untrusted, change one key, and replace the file atomically.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

STATE_FILE_NAME = ".claude.json"


def _load(state_file: Path) -> dict | None:
    try:
        data = json.loads(state_file.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _trusted(data: dict, folder: Path) -> bool:
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return False
    return any(
        isinstance(projects.get(str(p)), dict) and projects[str(p)].get("hasTrustDialogAccepted")
        for p in (folder, *folder.parents))


def is_trusted(folder: Path, state_file: Path) -> bool:
    """Read-only check of whether Claude Code trusts `folder` (or one of its ancestors)."""
    data = _load(state_file)
    return data is not None and _trusted(data, Path(folder).resolve())


def ensure_trusted(folder: Path, state_file: Path) -> bool:
    """Mark `folder` trusted if it isn't already. True when the file was written; never raises
    (a failure only means Claude Code shows its trust dialog). Only the one flag is ever added:
    a `projects` value or folder entry that isn't an object is left alone and nothing is written."""
    try:
        state_file = Path(state_file).resolve()  # write through a symlinked ~/.claude.json
    except (OSError, RuntimeError):  # a symlink loop (RuntimeError before Python 3.13)
        return False
    data = _load(state_file)
    folder = Path(folder).resolve()
    if data is None or _trusted(data, folder):
        return False
    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        return False  # not a shape we know; never replace Claude Code's project state
    entry = projects.setdefault(str(folder), {})
    if not isinstance(entry, dict):
        return False
    entry["hasTrustDialogAccepted"] = True
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=state_file.parent, prefix=f"{state_file.name}.",
                                         delete=False) as f:
            tmp = f.name
            f.write(json.dumps(data, indent=2))
        os.chmod(tmp, state_file.stat().st_mode & 0o7777)
        os.replace(tmp, state_file)
    except (OSError, ValueError):
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False
    return True
