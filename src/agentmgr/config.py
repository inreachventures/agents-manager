"""Paths and user configuration."""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _expand(p: str | Path) -> Path:
    return Path(os.path.expanduser(str(p))).resolve()


@dataclass(frozen=True)
class Config:
    home: Path
    code_roots: tuple[Path, ...]
    workstreams_dir: Path
    claude_cmd: str = "claude"
    tmux_socket: str = "agentmgr"
    extra: dict = field(default_factory=dict)
    repos: tuple[Path, ...] = ()  # single repos outside the code roots

    @property
    def config_file(self) -> Path:
        return self.home / "config.toml"

    @property
    def checkouts(self) -> tuple[Path, ...]:
        """Where the user's own checkouts live: read-only for the agent."""
        return self.code_roots + self.repos

    @property
    def db_path(self) -> Path:
        return self.home / "state.db"

    @property
    def memory_dir(self) -> Path:
        """Claude's auto memory, shared by every workstream session."""
        return self.home / "memory"

    @property
    def tmux_conf(self) -> Path:
        return self.home / "tmux.conf"


def load_config() -> Config:
    home = _expand(os.environ.get("AGENTMGR_HOME", "~/.agents-manager"))
    data: dict = {}
    cfg_file = home / "config.toml"  # Config.config_file
    if cfg_file.exists():
        data = tomllib.loads(cfg_file.read_text())
    roots = data.get("code_roots", ["~/code"])
    if env_roots := os.environ.get("AGENTMGR_CODE_ROOTS"):
        roots = env_roots.split(os.pathsep)
    ws_dir = os.environ.get("AGENTMGR_WORKSTREAMS_DIR", data.get("workstreams_dir", "~/workstreams"))
    return Config(
        home=home,
        code_roots=tuple(_expand(r) for r in roots),
        workstreams_dir=_expand(ws_dir),
        claude_cmd=os.environ.get("AGENTMGR_CLAUDE_CMD", data.get("claude_cmd", "claude")),
        tmux_socket=os.environ.get("AGENTMGR_TMUX_SOCKET", data.get("tmux_socket", "agentmgr")),
        extra=data,
        repos=tuple(_expand(r) for r in data.get("repos", [])),
    )


def _short(path: Path) -> str:
    home = Path.home()
    return "~/" + path.relative_to(home).as_posix() if path.is_relative_to(home) and path != home else str(path)


def save_paths(code_roots: list[Path], repos: list[Path]) -> None:
    """Write `code_roots` and `repos` to config.toml, leaving the rest of the file (comments too) as it is."""
    cfg = get_config()
    text = cfg.config_file.read_text() if cfg.config_file.exists() else ""
    values = {key: json.dumps([_short(p) for p in paths])
              for key, paths in (("code_roots", code_roots), ("repos", repos))}
    lines, out, done, i = text.splitlines(), [], set(), 0
    in_table = False
    while i < len(lines):
        line = lines[i]
        if re.match(r"\s*\[[^\[]", line):  # a [table] header: only top-level keys are ours
            in_table = True
        m = re.match(r"\s*(\w+)\s*=(.*)", line)
        if m and not in_table and m.group(1) in values:
            value = m.group(2)
            if value.strip().startswith("[") and "]" not in value:  # a list over several lines
                while i + 1 < len(lines) and "]" not in lines[i]:
                    i += 1
            out.append(f"{m.group(1)} = {values[m.group(1)]}")
            done.add(m.group(1))
        else:
            out.append(line)
        i += 1
    head = [f"{key} = {value}" for key, value in values.items() if key not in done]  # before any table
    new = "\n".join(head + out) + "\n"
    tomllib.loads(new)  # never write a file wm can't read back
    cfg.config_file.write_text(new)
    get_config.cache_clear()


@lru_cache(maxsize=1)
def get_config() -> Config:
    cfg = load_config()
    cfg.home.mkdir(parents=True, exist_ok=True)
    return cfg
