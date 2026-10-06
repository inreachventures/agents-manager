"""Paths and user configuration."""

from __future__ import annotations

import os
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
    cfg_file = home / "config.toml"
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
    )


@lru_cache(maxsize=1)
def get_config() -> Config:
    cfg = load_config()
    cfg.home.mkdir(parents=True, exist_ok=True)
    return cfg
