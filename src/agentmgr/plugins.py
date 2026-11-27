"""Finding plugins: read manifests, never import plugin code."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import Config, get_config
from .plugin import API_VERSION
from .templates import HOOK_EVENTS

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


def _view(raw: object) -> ViewSpec:
    if not isinstance(raw, dict):
        raise ValueError("each [[views]] entry must be a table")
    place = raw.get("place")
    if place not in ("main", "panel"):
        raise ValueError(f"view place must be 'main' or 'panel', not {place!r}")
    shortcut = raw.get("shortcut")
    if shortcut is not None and not isinstance(shortcut, str):
        raise ValueError("view shortcut must be text")
    return ViewSpec(id=_str(raw, "id", "view"), title=_str(raw, "title", "view"), place=place,
                    page=_str(raw, "page", "view"), shortcut=shortcut)


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
    views = data.get("views", [])
    if not isinstance(views, list):
        raise ValueError("views must be [[views]] tables")
    protect = data.get("protect", False)
    if not isinstance(protect, bool):
        raise ValueError("protect must be true or false")
    return Manifest(name=name, title=_str(data, "title", "plugin.toml"), version=_str(data, "version", "plugin.toml"),
                    api=api, path=folder, command=command, hooks=tuple(hooks),
                    views=tuple(_view(v) for v in views), protect=protect)


def _off(cfg: Config) -> list[str]:
    if not cfg.config_file.exists():
        return []
    off = tomllib.loads(cfg.config_file.read_text()).get("plugins", {}).get("off", [])
    return [n for n in off if isinstance(n, str)]


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
