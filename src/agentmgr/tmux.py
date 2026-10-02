"""Manager-owned tmux server (`tmux -L agentmgr`), separate from the user's own tmux."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import time
from importlib import resources
from pathlib import Path

from .config import Config, get_config

HOME_SESSION = "home"


class TmuxError(RuntimeError):
    pass


def available() -> bool:
    return shutil.which("tmux") is not None


def wm_executable() -> str:
    """Absolute path of `wm`, so tmux key bindings and hooks don't depend on PATH."""
    return shutil.which("wm") or str(Path(sys.argv[0]).resolve())


def _base(cfg: Config) -> list[str]:
    return ["tmux", "-L", cfg.tmux_socket, "-f", str(cfg.tmux_conf)]


def ensure_conf(cfg: Config | None = None) -> Path:
    cfg = cfg or get_config()
    conf = resources.files("agentmgr").joinpath("data/tmux.conf").read_text()
    conf = conf.replace("@WM@", shlex.quote(wm_executable()))
    if not cfg.tmux_conf.exists() or cfg.tmux_conf.read_text() != conf:
        cfg.tmux_conf.write_text(conf)
    return cfg.tmux_conf


def run(*args: str, check: bool = True, cfg: Config | None = None) -> subprocess.CompletedProcess:
    cfg = cfg or get_config()
    ensure_conf(cfg)
    proc = subprocess.run([*_base(cfg), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise TmuxError(f"tmux {' '.join(args)}: {proc.stderr.strip()}")
    return proc


def target(name: str) -> str:
    # "=" forces an exact session-name match
    return f"={name}"


def has_session(name: str) -> bool:
    if not available():
        return False
    return run("has-session", "-t", target(name), check=False).returncode == 0


def sessions() -> set[str]:
    if not available():
        return set()
    proc = run("list-sessions", "-F", "#{session_name}", check=False)
    return set(proc.stdout.split()) if proc.returncode == 0 else set()


def new_session(name: str, cwd: Path, command: list[str], env: dict[str, str] | None = None) -> None:
    args = ["new-session", "-d", "-s", name, "-c", str(cwd)]
    for k, v in (env or {}).items():
        args += ["-e", f"{k}={v}"]
    args.append(shlex.join(command))
    run(*args)


def kill_session(name: str) -> None:
    if has_session(name):
        run("kill-session", "-t", target(name), check=False)


def set_session_option(name: str, option: str, value: str) -> None:
    if has_session(name):
        run("set-option", "-t", f"{target(name)}:", option, value, check=False)  # bare "=name" is rejected here


def send_text(name: str, text: str) -> None:
    """Type `text` into the session's active pane and press Enter, as if you had typed a message to Claude."""
    pane = f"{target(name)}:"
    run("send-keys", "-t", pane, "-l", text)
    time.sleep(0.5)  # a separate, later Enter submits; sent with the text it can be taken as part of a paste
    run("send-keys", "-t", pane, "Enter")


def clients(name: str) -> list[str]:
    """TTYs of terminals currently viewing session `name`."""
    proc = run("list-clients", "-t", target(name), "-F", "#{client_tty}", check=False)
    return proc.stdout.split() if proc.returncode == 0 else []


def inside_manager() -> bool:
    """True when the current process runs inside a pane of the manager tmux server."""
    tmux_env = os.environ.get("TMUX", "")
    return bool(tmux_env) and Path(tmux_env.split(",")[0]).name == get_config().tmux_socket


def open_session(name: str) -> None:
    """Show session `name` in the current terminal: switch if already inside the manager, else attach."""
    if inside_manager():
        run("switch-client", "-t", target(name))
        return
    cfg = get_config()
    ensure_conf(cfg)
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}  # allow attaching from inside another tmux
    os.execvpe("tmux", [*_base(cfg), "attach-session", "-t", target(name)], env)


def switch_client(name: str, client: str | None = None) -> None:
    args = ["switch-client"]
    if client:
        args += ["-c", client]
    run(*args, "-t", target(name))


def ensure_home(command: list[str]) -> None:
    """The dashboard session. Re-created if its process exited."""
    if not has_session(HOME_SESSION):
        new_session(HOME_SESSION, Path.home(), command)
    run("set-option", "-w", "-t", f"{target(HOME_SESSION)}:", "pane-border-status", "off", check=False)
