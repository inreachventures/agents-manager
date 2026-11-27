"""Finding plugins (manifests, read without importing plugin code), loading them, and calling them in isolation."""

from __future__ import annotations

import hashlib
import importlib
import importlib.machinery
import importlib.util
import json
import re
import sys
import threading
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .config import Config, get_config
from .plugin import API_VERSION, Api, RepoInfo, WorkstreamInfo
from .templates import HOOK_EVENTS

if TYPE_CHECKING:
    from . import db

NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(frozen=True)
class ViewSpec:
    id: str
    title: str
    place: Literal["main", "panel"]
    page: str
    shortcut: str | None


@dataclass(frozen=True)
class Manifest:
    name: str
    title: str
    version: str
    api: int
    path: Path
    command: str | None
    hooks: tuple[str, ...]
    views: tuple[ViewSpec, ...]
    protect: bool


@dataclass(frozen=True)
class Skipped:
    name: str
    path: Path
    reason: str


def _str(data: dict, key: str, what: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} needs a text '{key}'")
    return value


def _view(raw: object, folder: Path) -> ViewSpec:
    if not isinstance(raw, dict):
        raise ValueError("each [[views]] entry must be a table")
    place = raw.get("place")
    if place not in ("main", "panel"):
        raise ValueError(f"view place must be 'main' or 'panel', not {place!r}")
    shortcut = raw.get("shortcut")
    if shortcut is not None and not isinstance(shortcut, str):
        raise ValueError("view shortcut must be text")
    page = _str(raw, "page", "view")
    if not (folder / page).resolve().is_relative_to(folder.resolve()):  # the app shows only the plugin's own files
        raise ValueError(f"view page {page!r} is outside the plugin folder")
    return ViewSpec(id=_str(raw, "id", "view"), title=_str(raw, "title", "view"), place=place, page=page,
                    shortcut=shortcut)


def _views(raw: object, folder: Path) -> tuple[ViewSpec, ...]:
    if not isinstance(raw, list):
        raise ValueError("views must be [[views]] tables")
    views = tuple(_view(v, folder) for v in raw)
    ids = [v.id for v in views]
    if twice := next((i for i in ids if ids.count(i) > 1), None):
        raise ValueError(f"view id {twice!r} is used twice")
    return views


def _parse(folder: Path) -> Manifest:
    """Read `folder/plugin.toml`; raise ValueError saying what is wrong with it."""
    try:
        data = tomllib.loads((folder / "plugin.toml").read_text())
    except OSError as e:
        raise ValueError(f"cannot read plugin.toml: {e.strerror or e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"plugin.toml is not valid TOML: {e}") from e
    name = _str(data, "name", "plugin.toml")
    if not NAME_RE.match(name):
        raise ValueError(f"plugin name {name!r} must match [a-z][a-z0-9-]*")
    if name != folder.name:
        raise ValueError(f"plugin name {name!r} must equal its folder name {folder.name!r}")
    api = data.get("api")
    if not isinstance(api, int) or isinstance(api, bool):
        raise ValueError("plugin.toml needs an integer 'api'")
    if api != API_VERSION:
        raise ValueError(f"written for plugin API {api}, wm has {API_VERSION}")
    command = data.get("command")
    if command is not None and not isinstance(command, str):
        raise ValueError("command must be text")
    hooks = data.get("hooks", [])
    if not isinstance(hooks, list) or not all(isinstance(h, str) for h in hooks):
        raise ValueError("hooks must be a list of event names")
    if unknown := [h for h in hooks if h not in HOOK_EVENTS]:
        raise ValueError(f"unknown hook event {', '.join(unknown)}")
    views = _views(data.get("views", []), folder)
    protect = data.get("protect", False)
    if not isinstance(protect, bool):
        raise ValueError("protect must be true or false")
    return Manifest(name=name, title=_str(data, "title", "plugin.toml"), version=_str(data, "version", "plugin.toml"),
                    api=api, path=folder, command=command, hooks=tuple(hooks),
                    views=views, protect=protect)


def read_manifest(folder: Path) -> Manifest | None:
    """The manifest in `folder`, or None if wm can't use it. For a plugin that is turned off, which `discover` doesn't
    read."""
    try:
        return _parse(folder)
    except ValueError:
        return None


def _off(cfg: Config) -> list[str]:
    if not cfg.config_file.exists():
        return []
    section = tomllib.loads(cfg.config_file.read_text()).get("plugins")
    off = section.get("off") if isinstance(section, dict) else None
    return [n for n in off if isinstance(n, str)] if isinstance(off, list) else []


def _folders(cfg: Config) -> list[Path]:
    root = cfg.plugins_dir
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))


def discover(cfg: Config | None = None) -> tuple[list[Manifest], list[Skipped]]:
    """Every plugin folder, as a manifest or a reason it was skipped. Sorted by name."""
    cfg = cfg or get_config()
    off = set(_off(cfg))
    manifests: list[Manifest] = []
    skipped: list[Skipped] = []
    for folder in _folders(cfg):
        if folder.name in off:
            skipped.append(Skipped(folder.name, folder, "turned off"))
            continue
        try:
            manifests.append(_parse(folder))
        except ValueError as e:
            skipped.append(Skipped(folder.name, folder, str(e)))
    return manifests, skipped


def set_enabled(name: str, on: bool, cfg: Config | None = None) -> None:
    """Record the choice as `[plugins] off = [...]` in config.toml, leaving the rest of the file as it is."""
    cfg = cfg or get_config()
    off = [n for n in _off(cfg) if n != name]
    if not on:
        off.append(name)
    line = f"off = {json.dumps(sorted(off))}"
    lines = cfg.config_file.read_text().splitlines() if cfg.config_file.exists() else []
    start = next((i for i, ln in enumerate(lines) if re.fullmatch(r"\s*\[plugins\]\s*(#.*)?", ln)), None)
    if start is None:
        new = lines + ([""] if lines else []) + ["[plugins]", line]
    else:
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"\s*\[", lines[i])), len(lines))
        key = next((i for i in range(start + 1, end) if re.match(r"\s*off\s*=", lines[i])), None)
        if key is None:
            new = lines[:start + 1] + [line] + lines[start + 1:]
        else:
            last = key
            if "]" not in lines[key]:  # a list over several lines
                while last + 1 < end and "]" not in lines[last]:
                    last += 1
            new = lines[:key] + [line] + lines[last + 1:]
    text = "\n".join(new) + "\n"
    tomllib.loads(text)  # never write a file wm can't read back
    cfg.config_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.config_file.write_text(text)


def fingerprint(cfg: Config | None = None) -> str:
    """Changes when a plugin is added, removed, edited or toggled."""
    cfg = cfg or get_config()
    h = hashlib.sha256()
    for folder in _folders(cfg):
        h.update(folder.name.encode())
        for fname in ("plugin.toml", "plugin.py"):
            try:
                st = (folder / fname).stat()
                h.update(f"|{fname}:{st.st_mtime_ns}:{st.st_size}".encode())
            except OSError:
                h.update(f"|{fname}:-".encode())
    h.update(json.dumps(sorted(_off(cfg))).encode())
    return h.hexdigest()


# Logging. Hooks point this at the hook log; the app-server leaves it on stderr.

def stderr_logger(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


_logger: Callable[[str], None] = stderr_logger


def set_logger(fn: Callable[[str], None]) -> None:
    global _logger
    _logger = fn


def log(msg: str) -> None:
    _logger(msg)


# Loading

@dataclass
class Loaded:
    manifest: Manifest
    api: Api  # holds what the plugin registered, in `api.registered`


@dataclass(frozen=True)
class HandlerError:
    """A loaded plugin's handler raised. The plugin stays loaded; this is not a skip."""

    name: str
    what: str  # e.g. "hook PostToolUse", "badge"
    reason: str  # "<Type>: <message>"


MODULE_PREFIX = "wm_plugin_"

_cache: dict[Path, Loaded | Skipped] = {}  # by plugin folder: loaded once per process
_errors: dict[tuple[str, str], HandlerError] = {}  # by (plugin, what): the latest, until it next succeeds
_lock = threading.RLock()  # the app-server loads and calls plugins from several threads


def module_name(name: str) -> str:
    return MODULE_PREFIX + name.replace("-", "_")


def _forget_modules(package: str) -> None:
    # list(): a copy taken in one step, as other threads of the app-server may import while we look
    for mod in [m for m in list(sys.modules) if m == package or m.startswith(package + ".")]:
        sys.modules.pop(mod, None)


def load(m: Manifest) -> Loaded | Skipped:
    """Import the plugin and call its `register(api)`. The folder becomes package `wm_plugin_<name>` with plugin.py
    as its `plugin` submodule, so a plugin's own modules import as `from . import util` and never clash with
    another plugin's. Any exception skips the plugin with the reason."""
    package = module_name(m.name)
    _forget_modules(package)
    try:
        spec = importlib.machinery.ModuleSpec(package, None, is_package=True)
        spec.submodule_search_locations = [str(m.path)]
        sys.modules[package] = importlib.util.module_from_spec(spec)
        module = importlib.import_module(f"{package}.plugin")
        register = getattr(module, "register", None)
        if not callable(register):
            raise TypeError("plugin.py has no register(api) function")
        api = Api(m)
        register(api)
    except (Exception, SystemExit) as e:  # a plugin calling sys.exit() must not end wm
        _forget_modules(package)
        return Skipped(m.name, m.path, f"{type(e).__name__}: {e}")
    return Loaded(m, api)


def loaded(*, event: str | None = None, command: str | None = None) -> list[Loaded]:
    """The plugins that load, in name order, imported only if they declare `event` / `command` when given. Each is
    loaded once per process; those that fail to load are in `skipped()`."""
    manifests, _ = discover()
    out: list[Loaded] = []
    for m in manifests:
        if event is not None and event not in m.hooks:
            continue
        if command is not None and m.command != command:
            continue
        with _lock:
            if m.path not in _cache:
                _cache[m.path] = load(m)
            result = _cache[m.path]
        if isinstance(result, Loaded):
            out.append(result)
    return out


def skipped() -> list[Skipped]:
    """Plugins that are skipped, in name order: a manifest wm can't use (bad TOML, wrong api, ...) and, among those
    `loaded()` has tried, the ones whose import or `register(api)` failed. Not plugins that are turned off, and not
    handlers that raised in a loaded plugin (those are in `errors()`)."""
    manifests, skips = discover()
    out = [s for s in skips if s.reason != "turned off"]
    with _lock:
        out += [r for m in manifests if isinstance(r := _cache.get(m.path), Skipped)]
    return sorted(out, key=lambda s: s.name)


def errors() -> list[HandlerError]:
    """Handlers of loaded plugins that raised: the latest error per (plugin, what), dropped once that same call next
    succeeds. In the order first seen."""
    with _lock:
        return list(_errors.values())


def reset() -> None:
    """Forget loaded plugins, load skips and handler errors, so the next `loaded()` imports them afresh."""
    with _lock:
        for result in _cache.values():
            _forget_modules(module_name(result.manifest.name if isinstance(result, Loaded) else result.name))
        _cache.clear()
        _errors.clear()


def call(lp: Loaded, what: str, fn: Callable[..., Any], *args: Any, budget: float | None = None) -> Any:
    """`fn(*args)`, isolated: an exception is logged, recorded in `errors()` and gives None; a success clears the
    error recorded for the same (plugin, what). A call over `budget` seconds is logged, never stopped."""
    name = lp.manifest.name
    start = time.monotonic()
    try:
        result = fn(*args)
    except (Exception, SystemExit) as e:
        reason = f"{type(e).__name__}: {e}"
        log(f"plugin {name}: {what}: {reason}")
        with _lock:
            _errors[(name, what)] = HandlerError(name, what, reason)
        return None
    else:
        with _lock:
            _errors.pop((name, what), None)
        return result
    finally:
        secs = time.monotonic() - start
        if budget is not None and secs > budget:
            log(f"plugin {name}: {what} took {secs:.2f}s")


def workstream_info(ws: db.Workstream, links: list[db.RepoLink]) -> WorkstreamInfo:
    """The read-only view of a workstream that plugin handlers get."""
    return WorkstreamInfo(key=ws.key, name=ws.name, ticket=ws.ticket, folder=ws.path, status=ws.status,
                          repos=tuple(RepoInfo(repo=link.repo, branch=link.branch, base=link.base,
                                               worktree=Path(link.worktree_path)) for link in links))


# What plugins add to a workstream's generated files and lifecycle

def record_error(name: str, what: str, reason: str) -> None:
    """Note a plugin problem that is not an exception, so `errors()` (and `wm doctor`) show it."""
    log(f"plugin {name}: {what}: {reason}")
    with _lock:
        _errors[(name, what)] = HandlerError(name, what, reason)


def call_text(lp: Loaded, what: str, fn: Callable[..., Any], *args: Any) -> str | None:
    """`call()` for a handler that returns text or None: anything else is recorded in `errors()` and dropped, as is
    an empty string."""
    result = call(lp, what, fn, *args)
    if result is not None and not isinstance(result, str):
        record_error(lp.manifest.name, what, f"returned {type(result).__name__}, not text")
        return None
    return result or None


def call_text_list(lp: Loaded, what: str, fn: Callable[..., Any], *args: Any) -> list[str]:
    """`call()` for a handler that returns a list of text: anything else is recorded in `errors()` and dropped."""
    result = call(lp, what, fn, *args)
    if result is None:
        return []
    if not isinstance(result, list) or not all(isinstance(x, str) for x in result):
        record_error(lp.manifest.name, what, f"returned {type(result).__name__}, not a list of text")
        return []
    return result


def claude_md_parts(ws: db.Workstream, links: list[db.RepoLink]) -> tuple[list[str], dict[str, str]]:
    """The plugin sections to append to CLAUDE.md, in plugin name order, and the named core sections plugins replace.
    Two plugins replacing the same section: the first by name wins, the other is recorded in `errors()`."""
    info = workstream_info(ws, links)
    sections: list[str] = []
    replacements: dict[str, str] = {}
    owner: dict[str, str] = {}
    for lp in loaded():
        name = lp.manifest.name
        reg = lp.api.registered
        if reg.claude_md and (text := call_text(lp, "claude_md", reg.claude_md, info)):
            sections.append(text)
        for section, fn in reg.replace.items():
            if section in owner:
                record_error(name, f"replaces {section}", f"already replaced by {owner[section]}")
            elif text := call_text(lp, f"replace {section}", fn, info):
                replacements[section] = text
                owner[section] = name
    return sections, replacements


def allowed_tools(ws: db.Workstream, links: list[db.RepoLink]) -> list[str]:
    """The tools plugins pre-approve for this workstream's session."""
    info = workstream_info(ws, links)
    out: list[str] = []
    for lp in loaded():
        if lp.api.registered.allowed_tools:
            out += call_text_list(lp, "allowed_tools", lp.api.registered.allowed_tools, info)
    return out


def on_archive(ws: db.Workstream, links: list[db.RepoLink]) -> None:
    """Tell each plugin the workstream is being archived, before its worktrees go."""
    info = workstream_info(ws, links)
    for lp in loaded():
        if lp.api.registered.on_archive:
            call(lp, "on_archive", lp.api.registered.on_archive, info)


def doctor_problems() -> list[str]:
    """Problems plugins report for `wm doctor`: failed loads, handler errors and each plugin's own checks."""
    out: list[str] = []
    for lp in loaded():  # first, so that skipped() includes plugins that fail to load
        if lp.api.registered.doctor:
            out += call_text_list(lp, "doctor", lp.api.registered.doctor)
    out = [f"{s.name}: skipped: {s.reason}" for s in skipped()] + out
    out += [f"{e.name}: {e.what}: {e.reason}" for e in errors()]
    return out
