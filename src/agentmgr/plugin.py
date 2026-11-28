"""The public surface plugins import. Nothing else in `agentmgr` is public.

A plugin's `plugin.py` defines `register(api: Api)` and registers its handlers on `api`. This module stays light:
hooks import it on every tool call, so anything heavy is imported inside the helper that needs it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .gitops import GitError

if TYPE_CHECKING:
    import typer

    from .plugins import Manifest

__all__ = [
    "API_VERSION", "REPLACEABLE", "TONES", "Api", "Badge", "GitError", "HookContext", "HookResult", "PluginError",
    "RepoInfo", "WorkstreamInfo",
]

API_VERSION = 1

# Core CLAUDE.md sections a plugin may replace with `api.replace_claude_md`.
REPLACEABLE = ("task_format", "intake_summary", "progress_rule")

TONES = ("", "red", "yellow", "blue", "green", "grey", "cyan", "orange", "purple", "amber")


class PluginError(Exception):
    """Raise for a clean, user-facing failure: `wm` prints it, a web view's request gets it as the error."""


@dataclass(frozen=True)
class RepoInfo:
    repo: str
    branch: str
    base: str
    worktree: Path


@dataclass(frozen=True)
class WorkstreamInfo:
    key: str
    name: str | None
    ticket: str | None
    folder: Path
    status: str
    repos: tuple[RepoInfo, ...]


@dataclass
class HookContext:
    ws: WorkstreamInfo
    event: str
    payload: dict  # Claude Code's hook JSON
    aux: bool  # the session is an auxiliary session of this plugin (see `Api.aux_env`)


@dataclass
class HookResult:
    context: str | None = None  # SessionStart, UserPromptSubmit, PostToolUse: added to Claude's context
    deny: str | None = None  # PreToolUse: refuse the tool call with this reason
    block: str | None = None  # PostToolUse, Stop: `decision: block` with this reason
    waiting_on: str | None = None  # Stop: "your turn" with this text, quietly


@dataclass
class Badge:
    text: str
    tone: str = ""
    help: str = ""

    def __post_init__(self) -> None:
        if self.tone not in TONES:
            raise ValueError(f"badge tone {self.tone!r} is not one of {', '.join(repr(t) for t in TONES)}")


@dataclass
class Registered:
    """What a plugin registered. Core reads it; plugins use the `Api` methods."""

    hooks: dict[str, Callable[[HookContext], HookResult | None]] = field(default_factory=dict)
    cli: typer.Typer | None = None
    requests: dict[str, Callable[..., Any]] = field(default_factory=dict)
    claude_md: Callable[[WorkstreamInfo], str | None] | None = None
    replace: dict[str, Callable[[WorkstreamInfo], str]] = field(default_factory=dict)
    allowed_tools: Callable[[WorkstreamInfo], list[str]] | None = None
    badge: Callable[[WorkstreamInfo], Badge | None] | None = None
    watch: Callable[[WorkstreamInfo], list[Path]] | None = None
    on_archive: Callable[[WorkstreamInfo], None] | None = None
    doctor: Callable[[], list[str]] | None = None
    every: list[tuple[float, Callable[[], None]]] = field(default_factory=list)


class Api:
    """Handed to a plugin's `register(api)`. Registration methods raise ValueError for a mistake, which skips the
    plugin with that reason."""

    GitError = GitError

    def __init__(self, manifest: Manifest) -> None:
        self.name = manifest.name
        self._events = manifest.hooks
        self._command = manifest.command
        self.registered = Registered()

    def _once(self, what: str, fn: Callable) -> Callable:
        if getattr(self.registered, what) is not None:
            raise ValueError(f"api.{what} registered twice")
        return fn

    # Registration

    def hook(self, event: str, fn: Callable[[HookContext], HookResult | None]) -> None:
        """Call `fn(ctx)` on this Claude Code hook event. The event must be in plugin.toml's `hooks`."""
        if event not in self._events:
            raise ValueError(f"hook {event} is not listed in plugin.toml's hooks")
        if event in self.registered.hooks:
            raise ValueError(f"hook {event} registered twice")
        self.registered.hooks[event] = fn

    def cli(self, app: typer.Typer) -> None:
        """Mount a Typer app as `wm <command> ...`, `command` coming from plugin.toml."""
        if not self._command:
            raise ValueError("api.cli needs a 'command' in plugin.toml")
        self.registered.cli = self._once("cli", app)

    def request(self, name: str, fn: Callable[..., Any]) -> None:
        """Make `fn(**params)` callable from this plugin's web views as `wm.request(name, params)`. Its return
        value must be JSON-serialisable."""
        if name in self.registered.requests:
            raise ValueError(f"request {name!r} registered twice")
        self.registered.requests[name] = fn

    def claude_md(self, fn: Callable[[WorkstreamInfo], str | None]) -> None:
        """`fn(ws)` returns markdown appended to the workstream's CLAUDE.md, or None."""
        self.registered.claude_md = self._once("claude_md", fn)

    def replace_claude_md(self, section: str, fn: Callable[[WorkstreamInfo], str]) -> None:
        """Replace a named core CLAUDE.md section (one of REPLACEABLE) with `fn(ws)`."""
        if section not in REPLACEABLE:
            raise ValueError(f"no replaceable CLAUDE.md section {section!r} (one of {', '.join(REPLACEABLE)})")
        if section in self.registered.replace:
            raise ValueError(f"section {section!r} replaced twice")
        self.registered.replace[section] = fn

    def allowed_tools(self, fn: Callable[[WorkstreamInfo], list[str]]) -> None:
        """`fn(ws)` returns tools pre-approved for the session, e.g. `["Bash(wm demo add WS-7 *)"]`."""
        self.registered.allowed_tools = self._once("allowed_tools", fn)

    def badge(self, fn: Callable[[WorkstreamInfo], Badge | None]) -> None:
        """`fn(ws)` returns a Badge for the sidebar row, or None. Runs on every state push: keep it cheap."""
        self.registered.badge = self._once("badge", fn)

    def watch(self, fn: Callable[[WorkstreamInfo], list[Path]]) -> None:
        """`fn(ws)` returns paths; when one changes, this plugin's web views are told."""
        self.registered.watch = self._once("watch", fn)

    def on_archive(self, fn: Callable[[WorkstreamInfo], None]) -> None:
        """`fn(ws)` runs when the workstream is archived, before its worktrees are removed."""
        self.registered.on_archive = self._once("on_archive", fn)

    def doctor(self, fn: Callable[[], list[str]]) -> None:
        """`fn()` returns problems for `wm doctor` to list (empty when all is well)."""
        self.registered.doctor = self._once("doctor", fn)

    def every(self, seconds: float, fn: Callable[[], None]) -> None:
        """Run `fn()` every `seconds` in the app-server."""
        if isinstance(seconds, bool) or not isinstance(seconds, int | float) or seconds <= 0:
            raise ValueError(f"api.every needs a positive number of seconds, not {seconds!r}")
        self.registered.every.append((seconds, fn))

    # Helpers

    def data_dir(self, ws: WorkstreamInfo) -> Path:
        """This plugin's folder in the workstream, `<folder>/.wm/<plugin>/`, created if missing."""
        d = ws.folder / ".wm" / self.name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def aux_env(self) -> dict[str, str]:
        """Environment marking a process this plugin starts as its auxiliary Claude session. Start that process (as
        any background process) with stdin, stdout and stderr redirected (`subprocess.DEVNULL` or a log file) and
        `start_new_session=True`: one started from a hook would otherwise write into, or hold open, the streams
        Claude Code reads the hook's answer from."""
        return {"AGENTMGR_AUX": self.name}

    def workstream(self, key: str) -> WorkstreamInfo:
        """The active workstream `key`. Raises PluginError if there is none or it is archived."""
        if not isinstance(key, str):
            raise PluginError(f"a workstream key is text, not {type(key).__name__}")
        from . import db
        from . import workstream as ws_mod
        from .plugins import workstream_info

        conn = db.connect()
        try:
            ws = ws_mod.require(conn, key)
        except ws_mod.WorkstreamError as e:
            raise PluginError(str(e)) from e
        return workstream_info(ws, db.list_repos(conn, ws.key))

    def workstreams(self) -> list[WorkstreamInfo]:
        """Every active workstream, oldest first."""
        from . import db
        from .plugins import workstream_info

        conn = db.connect()
        return [workstream_info(ws, db.list_repos(conn, ws.key)) for ws in db.list_workstreams(conn)]

    def repo_diff(self, repo: RepoInfo) -> dict:
        """Everything the branch changes since it forked from its base, as core's diff view shows it."""
        from . import diffs

        return diffs.repo_diff(repo.worktree, repo.base)

    def git(self, cwd: Path, *args: str, check: bool = True) -> str:
        """Run git in `cwd` and return its stdout. Raises GitError when it fails and `check` is set."""
        from . import gitops

        return gitops.git(cwd, *args, check=check)

    def wm_executable(self) -> str:
        """Absolute path of the `wm` to call back into."""
        from . import tmux

        return tmux.wm_executable()

    def claude_command(self) -> str:
        """The command that starts Claude Code (`claude` unless configured otherwise)."""
        from .config import get_config

        return get_config().claude_cmd

    def claude_missing(self) -> bool:
        """True when the Claude Code command isn't installed."""
        from . import workstream as ws_mod

        return ws_mod.claude_missing() is not None

    def log(self, msg: str) -> None:
        """Write a line to wm's log (the hook log in hooks, stderr in the app-server)."""
        from . import plugins

        plugins.log(f"plugin {self.name}: {msg}")
