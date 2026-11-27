"""A small "notes" plugin that uses every extension point. Tests drive it through files and environment variables:

DEMO_RAISE=1     the PostToolUse handler raises RuntimeError("boom")
DEMO_REPLACE=1   replaces the core "progress_rule" CLAUDE.md section
DEMO_DOCTOR=1    doctor reports "demo: example problem"
DEMO_MARKER=dir  on_archive writes dir/archived.txt; the every() job counts in dir/ticks.txt
DEMO_IMPORT_MARK=file   importing this module touches the file

In the workstream's data dir (.wm/demo/): inbox.txt is handed to Claude after a tool call and removed; job.txt makes
Stop wait on it quietly; block.txt makes Stop keep Claude going.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from agentmgr.plugin import Api, Badge, HookContext, HookResult, WorkstreamInfo

if mark := os.environ.get("DEMO_IMPORT_MARK"):
    Path(mark).touch()

api: Api
cli = typer.Typer(no_args_is_help=True)


@cli.callback()
def main() -> None:
    """Notes kept with a workstream."""


def _file(ws: WorkstreamInfo) -> Path:
    return api.data_dir(ws) / "notes.json"


def _notes(ws: WorkstreamInfo) -> list[str]:
    f = _file(ws)
    return json.loads(f.read_text()) if f.exists() else []


def _add(ws: WorkstreamInfo, text: str) -> None:
    _file(ws).write_text(json.dumps([*_notes(ws), text]))


@cli.command()
def add(key: str, text: str) -> None:
    """Add a note to workstream KEY."""
    _add(api.workstream(key), text)
    typer.echo("noted")


def notes(key: str) -> list[str]:
    return _notes(api.workstream(key))


def add_note(key: str, text: str) -> dict:
    _add(api.workstream(key), text)
    return {}


def section(ws: WorkstreamInfo) -> str:
    lines = [f"- {n}" for n in _notes(ws)] or ["(none yet)"]
    return "\n".join(["## Demo notes", "", f"Add one with `wm demo add {ws.key} <text>`.", "", *lines])


def badge(ws: WorkstreamInfo) -> Badge | None:
    n = len(_notes(ws))
    return Badge(text=str(n), tone="blue", help=f"{n} demo note{'s' if n != 1 else ''}") if n else None


def on_archive(ws: WorkstreamInfo) -> None:
    if marker := os.environ.get("DEMO_MARKER"):
        (Path(marker) / "archived.txt").write_text(ws.key)


def doctor() -> list[str]:
    return ["demo: example problem"] if os.environ.get("DEMO_DOCTOR") == "1" else []


def tick() -> None:
    if marker := os.environ.get("DEMO_MARKER"):
        f = Path(marker) / "ticks.txt"
        f.write_text(str(int(f.read_text()) + 1 if f.exists() else 1))


def _take(f: Path) -> str | None:
    if not f.exists():
        return None
    text = f.read_text()
    f.unlink()
    return text


def post_tool_use(ctx: HookContext) -> HookResult | None:
    if os.environ.get("DEMO_RAISE") == "1":
        raise RuntimeError("boom")
    text = _take(api.data_dir(ctx.ws) / "inbox.txt")
    return HookResult(context=text) if text is not None else None


def stop(ctx: HookContext) -> HookResult | None:
    d = api.data_dir(ctx.ws)
    if (d / "block.txt").exists():
        return HookResult(block=(d / "block.txt").read_text())
    if (d / "job.txt").exists():
        return HookResult(waiting_on=(d / "job.txt").read_text())
    return None


def register(a: Api) -> None:
    global api
    api = a
    api.hook("PostToolUse", post_tool_use)
    api.hook("Stop", stop)
    api.cli(cli)
    api.request("notes", notes)
    api.request("add_note", add_note)
    api.claude_md(section)
    if os.environ.get("DEMO_REPLACE") == "1":
        api.replace_claude_md("progress_rule", lambda ws: "- Keep notes with `wm demo add`, not in TASK.md.")
    api.allowed_tools(lambda ws: [f"Bash(wm demo add {ws.key} *)"])
    api.badge(badge)
    api.watch(lambda ws: [_file(ws)])
    api.on_archive(on_archive)
    api.doctor(doctor)
    api.every(1, tick)
