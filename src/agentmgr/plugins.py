"""Finding plugins (manifests, read without importing plugin code), loading them, and calling them in isolation."""

from __future__ import annotations

import contextlib
import importlib
import importlib.machinery
import importlib.util
import json
import math
import os
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
from .plugin import API_VERSION, Api, PluginError, RepoInfo, WorkstreamInfo
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
    """Record the choice as `[plugins] off = [...]` in config.toml, leaving the rest of the file as it is. Raises
    PluginError, leaving the file alone, when it is written in a way this line editor can't change (a dotted
    `plugins.off` key, an inline table, a list with a "]" in a comment, ...)."""
    cfg = cfg or get_config()
    try:
        off = [n for n in _off(cfg) if n != name]
    except (OSError, ValueError) as e:
        raise PluginError(f"cannot read {cfg.config_file}: {e}") from e
    if not on:
        off.append(name)
    off = sorted(set(off))
    line = f"off = {json.dumps(off)}"
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
    try:  # never write a file wm can't read back, or that doesn't say what was asked
        section = tomllib.loads(text).get("plugins")
        written = section.get("off") if isinstance(section, dict) else None
    except tomllib.TOMLDecodeError:
        written = None
    if written != off:
        raise PluginError(f"cannot turn {name} {'on' if on else 'off'}: wm can't edit the [plugins] table in "
                          f"{cfg.config_file}. Write it as a [plugins] section with an off = [...] list of the "
                          "plugins to turn off, on one line or one name per line.")
    cfg.config_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.config_file.write_text(text)


def fingerprint(cfg: Config | None = None) -> str:
    """Changes when a plugin is added, removed, edited or toggled."""
    import hashlib

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
    """A loaded plugin's handler raised or returned something wm can't use. The plugin stays loaded; this is not a
    skip."""

    name: str
    what: str  # e.g. "hook PostToolUse", "badge"
    reason: str  # "<Type>: <message>", or what was wrong with the result
    at: float  # time.time() when it was recorded


class WrongResult(Exception):
    """Raised by a `convert` given to `call()`: the handler returned something wm can't use. The message says what."""


MODULE_PREFIX = "wm_plugin_"
ERRORS_FILE = "plugin-errors.json"  # in the wm home, not the plugins folder: the app-server restarts when that changes
REWRITE_SECONDS = 60.0  # the same error again rewrites its entry's time at most this often

_cache: dict[Path, Loaded | Skipped] = {}  # by plugin folder: loaded once per process
_errors: dict[tuple[str, str], HandlerError] = {}  # by (plugin, what): this process's, until that call next succeeds
_persisted: dict[tuple[str, str], HandlerError] | None = None  # the errors file as this process last read or wrote it
_dirty: set[tuple[str, str]] = set()  # (plugin, what) whose error changed here since the last flush()
_fresh: set[tuple[str, str]] = set()  # (plugin, what) that failed since the last flush(): a success doesn't clear them
_batched = False  # batch_errors(): written by flush() only
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


def loaded(*, event: str | None = None, command: str | None = None, name: str | None = None) -> list[Loaded]:
    """The plugins that load, in name order, imported only if they declare `event` / `command` and are called `name`,
    when given. Each is loaded once per process; those that fail to load are in `skipped()`."""
    manifests, _ = discover()
    out: list[Loaded] = []
    for m in manifests:
        if event is not None and event not in m.hooks:
            continue
        if command is not None and m.command != command:
            continue
        if name is not None and m.name != name:
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
    """This process's handler errors: the latest per (plugin, what), dropped once that same call next succeeds. In the
    order first seen. `persisted_errors()` has those of every wm process."""
    with _lock:
        return list(_errors.values())


def reset() -> None:
    """Forget loaded plugins, load skips and this process's handler errors (not the errors file), so the next
    `loaded()` imports them afresh."""
    global _persisted
    with _lock:
        for result in _cache.values():
            _forget_modules(module_name(result.manifest.name if isinstance(result, Loaded) else result.name))
        _cache.clear()
        _errors.clear()
        _dirty.clear()
        _fresh.clear()
        _persisted = None


# Handler errors, kept in <wm home>/plugin-errors.json: hooks, the app-server and `wm` commands each run in their own
# process, and `wm doctor` and the app must see what the others ran into. Hooks run on every tool call, so a handler
# that succeeds costs no file access unless this process has reason to think an entry exists: it reads the file at
# most once, the first time it needs to know. Writes replace the file atomically; two processes writing at once may
# lose one's change (the last writer wins), which only delays an entry until that handler next runs. The app-server
# calls badges and watches for every workstream every few seconds, so it batches: its changes are written once per
# state push (`flush()`), not on every call.

def _errors_path() -> Path:
    return get_config().home / ERRORS_FILE


def _read_errors_file() -> dict[tuple[str, str], HandlerError]:
    """The entries in the errors file. Missing, unreadable or not what wm writes: empty, never an exception."""
    try:
        raw = json.loads(_errors_path().read_text())
    except Exception:  # noqa: BLE001 - also RecursionError, from JSON nested deep enough
        return {}
    out: dict[tuple[str, str], HandlerError] = {}
    for e in raw if isinstance(raw, list) else []:
        if not isinstance(e, dict):
            continue
        name, what, reason, at = e.get("plugin"), e.get("what"), e.get("reason"), e.get("at")
        if (all(isinstance(v, str) for v in (name, what, reason))
                and isinstance(at, int | float) and not isinstance(at, bool)):
            try:
                stamp = float(at)
            except OverflowError:  # an int too big for a float
                continue
            if math.isfinite(stamp):
                out[(name, what)] = HandlerError(name, what, reason, stamp)
    return out


def _write_errors_file(entries: dict[tuple[str, str], HandlerError]) -> None:
    """Replace the errors file with `entries`, atomically. A failure is logged, never raised."""
    import tempfile  # here: only an error path writes, and hooks import this module on every tool call

    path = _errors_path()
    tmp: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{ERRORS_FILE}.", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump([{"plugin": e.name, "what": e.what, "reason": e.reason, "at": e.at} for e in entries.values()],
                      f, indent=1)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001 - a plugin's error must never become core's
        log(f"plugins: cannot write {path}: {type(e).__name__}: {e}")
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def batch_errors() -> None:
    """From now on, write this process's error changes only when `flush()` is called (the app-server, per state
    push)."""
    global _batched
    _batched = True


def flush() -> None:
    """Write this process's error changes since the last flush to the errors file: re-read first, so that what other
    processes wrote since is kept, and written only if something changed. An error that is still the same is written
    again only when its time is REWRITE_SECONDS old, as a badge or job can fail every few seconds."""
    global _persisted
    with _lock:
        if not _dirty:
            return
        if _persisted is None:
            _persisted = _read_errors_file()
        changes: dict[tuple[str, str], HandlerError | None] = {}
        for key in _dirty:
            mem, old = _errors.get(key), _persisted.get(key)
            if mem is None:
                if old is not None:
                    changes[key] = None
            elif old is None or old.reason != mem.reason or mem.at - old.at >= REWRITE_SECONDS:
                changes[key] = mem
        _dirty.clear()
        _fresh.clear()
        if not changes:
            return
        entries = _read_errors_file()
        before = dict(entries)
        for key, entry in changes.items():
            if entry is None:
                entries.pop(key, None)
            else:
                entries[key] = entry
        if entries != before:
            _write_errors_file(entries)
        _persisted = entries


REASON_MAX = 300  # characters of a reason kept and shown: a line in `wm doctor` and an app notice


def _one_line(reason: str) -> str:
    text = " ".join(reason.split())
    return text if len(text) <= REASON_MAX else text[:REASON_MAX - 1] + "…"


def record_error(name: str, what: str, reason: str) -> None:
    """Log and keep a plugin's handler error, for `errors()`, `persisted_errors()`, `wm doctor` and the app. The
    reason is made one line of at most REASON_MAX characters."""
    reason = _one_line(reason)
    log(f"plugin {name}: {what}: {reason}")
    with _lock:
        _errors[(name, what)] = HandlerError(name, what, reason, time.time())
        _dirty.add((name, what))
        _fresh.add((name, what))
        if not _batched:
            flush()


def _succeeded(name: str, what: str) -> None:
    """Clear the error of (plugin, what), if this process recorded one or the errors file has one. Batched, an error
    recorded since the last flush stays: a badge that fails for one workstream and works for the next has an error."""
    global _persisted
    key = (name, what)
    with _lock:
        if key in _fresh:
            return
        if _errors.pop(key, None) is None:
            if _persisted is None:
                _persisted = _read_errors_file()
            if key not in _persisted:
                return
        _dirty.add(key)
        if not _batched:
            flush()


def _installed_names(cfg: Config) -> tuple[set[str], set[str]]:
    """The plugins in the plugins folder, and those of them turned off."""
    names = {f.name for f in _folders(cfg)}
    try:
        off = set(_off(cfg))
    except (OSError, ValueError):  # config.toml unreadable: nothing is known to be off
        off = set()
    return names, off & names


def persisted_errors() -> list[HandlerError]:
    """The handler errors of every wm process that have not been cleared since, this process's own included, by
    plugin name. Entries of plugins no longer installed are dropped from the file; those of plugins turned off are
    left out but kept."""
    global _persisted
    cfg = get_config()
    installed, off = _installed_names(cfg)
    with _lock:
        flush()
        entries = _read_errors_file()
        if gone := [k for k in entries if k[0] not in installed]:
            for k in gone:
                del entries[k]
            _write_errors_file(entries)
        _persisted = dict(entries)
        entries.update(_errors)  # this process's: newer, and kept even if the file can't be written
    return sorted((e for e in entries.values() if e.name in installed and e.name not in off),
                  key=lambda e: (e.name, e.what))


def ago(at: float, now: float | None = None) -> str:
    """How long ago `at` was, roughly: "just now", "5m ago", "3h ago", "2d ago"."""
    secs = max(0.0, (time.time() if now is None else now) - at)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit} ago"
    return "just now"


def call(lp: Loaded, what: str, fn: Callable[..., Any], *args: Any, budget: float | None = None,
         convert: Callable[[Any], Any] | None = None) -> Any:
    """`fn(*args)`, isolated: an exception is recorded (`record_error`) and gives None. `convert(result)` checks the
    result and gives what to use instead; when it raises (`WrongResult` with what is wrong, or any other exception),
    that is recorded the same way and gives None. A success clears the error recorded for the same (plugin, what).
    A call over `budget` seconds is logged, never stopped."""
    name = lp.manifest.name
    start = time.monotonic()
    try:
        result = fn(*args)
    except (Exception, SystemExit) as e:
        record_error(name, what, f"{type(e).__name__}: {e}")
        return None
    finally:
        secs = time.monotonic() - start
        if budget is not None and secs > budget:
            log(f"plugin {name}: {what} took {secs:.2f}s")
    if convert is not None:
        try:
            result = convert(result)
        except WrongResult as e:
            record_error(name, what, str(e))
            return None
        except Exception as e:  # noqa: BLE001 - checking the result runs plugin code too (a generator, a property)
            record_error(name, what, f"{type(e).__name__}: {e}")
            return None
    _succeeded(name, what)
    return result


def workstream_info(ws: db.Workstream, links: list[db.RepoLink]) -> WorkstreamInfo:
    """The read-only view of a workstream that plugin handlers get."""
    return WorkstreamInfo(key=ws.key, name=ws.name, ticket=ws.ticket, folder=ws.path, status=ws.status,
                          repos=tuple(RepoInfo(repo=link.repo, branch=link.branch, base=link.base,
                                               worktree=Path(link.worktree_path)) for link in links))


# What plugins add to a workstream's generated files and lifecycle

CLAUDE_MD_BUDGET = 0.5  # seconds a CLAUDE.md or allowed-tools handler may take before it is logged: it runs in hooks


def _text(result: Any) -> str | None:
    if result is not None and not isinstance(result, str):
        raise WrongResult(f"returned {type(result).__name__}, not text")
    return result or None


def _text_list(result: Any) -> list[str]:
    if result is None:
        return []
    if not isinstance(result, list) or not all(isinstance(x, str) for x in result):
        raise WrongResult(f"returned {type(result).__name__}, not a list of text")
    return result


def call_text(lp: Loaded, what: str, fn: Callable[..., Any], *args: Any, budget: float | None = None) -> str | None:
    """`call()` for a handler that returns text or None: anything else is recorded as its error and dropped, as is an
    empty string."""
    return call(lp, what, fn, *args, budget=budget, convert=_text)


def call_text_list(lp: Loaded, what: str, fn: Callable[..., Any], *args: Any,
                   budget: float | None = None) -> list[str]:
    """`call()` for a handler that returns a list of text: anything else is recorded as its error and dropped."""
    return call(lp, what, fn, *args, budget=budget, convert=_text_list) or []


def claude_md_parts(ws: db.Workstream, links: list[db.RepoLink]) -> tuple[list[str], dict[str, str]]:
    """The plugin sections to append to CLAUDE.md, in plugin name order, and the named core sections plugins replace.
    Two plugins replacing the same section: the first by name wins, the other is recorded as its error until it no
    longer clashes."""
    info = workstream_info(ws, links)
    sections: list[str] = []
    replacements: dict[str, str] = {}
    owner: dict[str, str] = {}
    for lp in loaded():
        name = lp.manifest.name
        reg = lp.api.registered
        if reg.claude_md and (text := call_text(lp, "claude_md", reg.claude_md, info, budget=CLAUDE_MD_BUDGET)):
            sections.append(text)
        for section, fn in reg.replace.items():
            what = f"replace {section}"
            if section in owner:
                record_error(name, what, f"already replaced by {owner[section]}")
            elif text := call_text(lp, what, fn, info, budget=CLAUDE_MD_BUDGET):
                replacements[section] = text
                owner[section] = name
    return sections, replacements


def allowed_tools(ws: db.Workstream, links: list[db.RepoLink]) -> list[str]:
    """The tools plugins pre-approve for this workstream's session. They are passed joined by commas, so an entry with
    a comma in it would split into others: it is left out and recorded as the plugin's error."""
    info = workstream_info(ws, links)
    out: list[str] = []
    for lp in loaded():
        if lp.api.registered.allowed_tools:
            tools = call_text_list(lp, "allowed_tools", lp.api.registered.allowed_tools, info,
                                   budget=CLAUDE_MD_BUDGET)
            if bad := [t for t in tools if "," in t]:
                record_error(lp.manifest.name, "allowed_tools",
                             f"{', '.join(map(repr, bad))} {'has a comma' if len(bad) == 1 else 'have commas'}, "
                             "which --allowedTools can't pass on")
            out += [t for t in tools if "," not in t]
    return out


def on_archive(ws: db.Workstream, links: list[db.RepoLink]) -> None:
    """Tell each plugin the workstream is being archived, before its worktrees go."""
    info = workstream_info(ws, links)
    for lp in loaded():
        if lp.api.registered.on_archive:
            call(lp, "on_archive", lp.api.registered.on_archive, info)


def doctor_problems() -> list[str]:
    """Problems plugins report for `wm doctor`: failed loads, handler errors of any wm process (with how long ago) and
    each plugin's own checks."""
    out: list[str] = []
    for lp in loaded():  # first, so that skipped() includes plugins that fail to load
        if lp.api.registered.doctor:
            out += call_text_list(lp, "doctor", lp.api.registered.doctor)
    out = [f"{s.name}: skipped: {s.reason}" for s in skipped()] + out
    out += [f"{e.name}: {e.what}: {e.reason} ({ago(e.at)})" for e in persisted_errors()]
    return out
