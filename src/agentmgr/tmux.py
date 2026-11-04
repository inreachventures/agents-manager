"""Manager-owned tmux server (`tmux -L agentmgr`), separate from the user's own tmux."""

from __future__ import annotations

import hashlib
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
# The native app shows a workstream through a session grouped with it (same window, so the same Claude), named
# <key>_app: there tmux's status line is off and the client detaches when Claude exits. Keys never contain "_".
VIEW_SUFFIX = "_app"
APP_ACTIVE = "@wm_app_active"  # global option: "1" while the native app is the frontmost app
WM_ENV = "AGENTMGR_WM"  # set by the launcher of the wm inside the Mac app, to its own path


class TmuxError(RuntimeError):
    pass


def available() -> bool:
    return shutil.which("tmux") is not None


def wm_executable() -> str:
    """Absolute path of `wm`, so tmux key bindings and hooks don't depend on PATH. The wm inside the Mac app names
    itself: hooks it writes keep calling it even when another wm is on PATH."""
    return os.environ.get(WM_ENV) or shutil.which("wm") or str(Path(sys.argv[0]).resolve())


def bundled() -> bool:
    """This is the wm inside the Mac app, usually not on the user's PATH."""
    return bool(os.environ.get(WM_ENV))


def _base(cfg: Config) -> list[str]:
    return ["tmux", "-L", cfg.tmux_socket, "-f", str(cfg.tmux_conf)]


def ensure_conf(cfg: Config | None = None) -> Path:
    cfg = cfg or get_config()
    conf = resources.files("agentmgr").joinpath("data/tmux.conf").read_text()
    conf = conf.replace("@WM@", shlex.quote(wm_executable()))
    if not cfg.tmux_conf.exists() or cfg.tmux_conf.read_text() != conf:
        cfg.tmux_conf.write_text(conf)
    return cfg.tmux_conf


def reload_conf() -> None:
    """Re-read the config into a running server: one started by an older wm lacks the app-aware key bindings."""
    if sessions():
        run("source-file", str(ensure_conf()), check=False)


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
    """Kill session `name` and every session grouped with it, such as the app's view, even renamed: a grouped
    session keeps the shared window, and so Claude, alive."""
    groups = _groups()
    group = groups.get(name)
    for session, g in groups.items():
        if session in (name, view_name(name)) or (group and g == group):
            run("kill-session", "-t", target(session), check=False)


def _groups() -> dict[str, str]:
    """Session name -> its session group ("" when not grouped)."""
    if not available():
        return {}
    proc = run("list-sessions", "-F", "#{session_name}\t#{session_group}", check=False)
    pairs = [line.partition("\t") for line in proc.stdout.splitlines()] if proc.returncode == 0 else []
    return {name: group for name, _, group in pairs}


def view_name(name: str) -> str:
    return name + VIEW_SUFFIX


def _stale_view(view: str, groups: dict[str, str]) -> bool:
    """A view not grouped with its workstream's current session: that session is gone, or was started again."""
    base = view.removesuffix(VIEW_SUFFIX)
    return base not in groups or not groups[base] or groups[base] != groups[view]


def ensure_view(name: str) -> str:
    """The native app's session for workstream session `name` (see VIEW_SUFFIX); returns its name."""
    view = view_name(name)
    groups = _groups()
    if view in groups and _stale_view(view, groups):
        run("kill-session", "-t", target(view), check=False)
        del groups[view]
    if view not in groups:
        run("new-session", "-d", "-t", target(name), "-s", view)
        set_session_option(view, "status", "off")
        set_session_option(view, "detach-on-destroy", "on")
    return view


def prune_views() -> None:
    """Kill app views left without their workstream's session (killed by an older wm, say), so Claude doesn't
    live on in them."""
    groups = _groups()
    for session in groups:
        if session.endswith(VIEW_SUFFIX) and _stale_view(session, groups):
            run("kill-session", "-t", target(session), check=False)


def set_app_active(active: bool) -> None:
    if sessions():
        run("set-option", "-g", APP_ACTIVE, "1" if active else "0", check=False)


def client_sessions() -> dict[int, str]:
    """Client process id -> the session it shows."""
    proc = run("list-clients", "-F", "#{client_pid}\t#{session_name}", check=False) if available() else None
    pairs = [line.partition("\t") for line in proc.stdout.splitlines()] if proc and proc.returncode == 0 else []
    return {int(pid): session for pid, _, session in pairs if pid.isdigit()}


def attach_command(name: str) -> list[str]:
    """argv that attaches a terminal to session `name`, for the native app's terminal view."""
    cfg = get_config()
    ensure_conf(cfg)
    return [shutil.which("tmux") or "tmux", "-u", *_base(cfg)[1:], "attach-session", "-t", target(name)]


def client_of(pid: int) -> str | None:
    """Name of the tmux client whose process is `pid`."""
    proc = run("list-clients", "-F", "#{client_pid}\t#{client_name}", check=False)
    for line in proc.stdout.splitlines():
        client_pid, _, name = line.partition("\t")
        if client_pid == str(pid):
            return name
    return None


def set_session_option(name: str, option: str, value: str) -> None:
    if has_session(name):
        run("set-option", "-t", f"{target(name)}:", option, value, check=False)  # bare "=name" is rejected here


def set_window_option(name: str, option: str, value: str) -> None:
    """On the session's window, so the app's view of the session (same window) shows it too."""
    if has_session(name):
        run("set-option", "-w", "-t", f"{target(name)}:", option, value, check=False)


def session_option(name: str, option: str) -> str:
    return run("show-options", "-v", "-q", "-t", f"{target(name)}:", option, check=False).stdout.strip()


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


def watched(name: str) -> bool:
    """Someone is looking at session `name`: a terminal shows it, or the native app shows it and is in front."""
    shown = set(client_sessions().values())
    if name in shown:
        return True
    active = run("show-options", "-g", "-v", "-q", APP_ACTIVE, check=False).stdout.strip() == "1"
    return view_name(name) in shown and active


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


def code_version() -> str:
    """Fingerprint of wm's code on disk, to spot a dashboard still running an older version."""
    pkg = Path(__file__).parent
    digest = hashlib.sha1()
    for f in sorted(f for f in pkg.rglob("*") if f.is_file() and "__pycache__" not in f.parts):
        digest.update(f.relative_to(pkg).as_posix().encode() + b"\0" + f.read_bytes())
    return digest.hexdigest()[:12]


def ensure_home(command: list[str]) -> None:
    """The dashboard session. Re-created if its process exited, restarted if it runs older code and no terminal is
    showing it (`q` only detaches, so reopening would otherwise bring back the old dashboard)."""
    version = code_version()
    if not has_session(HOME_SESSION):
        new_session(HOME_SESSION, Path.home(), command)
        set_session_option(HOME_SESSION, "@wm_code", version)
    elif session_option(HOME_SESSION, "@wm_code") != version and not clients(HOME_SESSION):
        run("respawn-pane", "-k", "-t", f"{target(HOME_SESSION)}:", "-c", str(Path.home()), shlex.join(command))
        set_session_option(HOME_SESSION, "@wm_code", version)
    run("set-option", "-w", "-t", f"{target(HOME_SESSION)}:", "pane-border-status", "off", check=False)
