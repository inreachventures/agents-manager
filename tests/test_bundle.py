"""The wm inside the Mac app: not on PATH, and it moves when the app does."""

import json
import shlex

from agentmgr import tmux
from agentmgr import workstream as ws_mod


def hook_wm(ws):
    settings = json.loads((ws.path / ".claude" / "settings.json").read_text())
    return {shlex.split(entry["hooks"][0]["command"])[0] for [entry] in settings["hooks"].values()}


def fake_wm(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_the_bundled_wm_calls_itself_from_hooks(env, monkeypatch, tmp_path):
    wm = fake_wm(tmp_path / "Workstreams.app" / "Contents" / "Resources" / "wm" / "bin" / "wm")
    monkeypatch.setenv(tmux.WM_ENV, str(wm))
    ws = ws_mod.new(launch=False, name="side work")
    assert hook_wm(ws) == {str(wm)}
    assert f"{wm} next" in tmux.ensure_conf().read_text()  # tmux key bindings


def app_wm(root):
    return fake_wm(root / "Workstreams.app" / "Contents" / "Resources" / "wm" / "bin" / "wm")


def made_by(monkeypatch, wm, name):
    monkeypatch.setenv(tmux.WM_ENV, str(wm))
    return ws_mod.new(launch=False, name=name)


def test_hooks_that_call_a_wm_that_is_gone_are_repointed(env, monkeypatch, tmp_path):
    monkeypatch.setattr(ws_mod, "VOLUMES", f"{tmp_path / 'Volumes'}/")  # where disk images mount
    gone = fake_wm(tmp_path / "old-checkout" / ".venv" / "bin" / "wm")
    moved = made_by(monkeypatch, gone, "moved work")
    on_image = made_by(monkeypatch, app_wm(tmp_path / "Volumes" / "Workstreams"), "first work")
    installed = fake_wm(tmp_path / "opt" / "bin" / "wm")
    kept = made_by(monkeypatch, installed, "other work")
    gone.unlink()

    here = app_wm(tmp_path / "Applications")
    monkeypatch.setenv(tmux.WM_ENV, str(here))
    assert ws_mod.repoint_hooks() == [moved.key, on_image.key]
    assert hook_wm(moved) == hook_wm(on_image) == {str(here)}
    assert hook_wm(kept) == {str(installed)}  # a wm the user installed, still there: theirs
    assert ws_mod.repoint_hooks() == []

    monkeypatch.delenv(tmux.WM_ENV)  # that installed wm, run from Terminal, leaves the app's hooks alone
    monkeypatch.setenv("PATH", f"{installed.parent}:/usr/bin:/bin")
    assert ws_mod.repoint_hooks() == []


def test_hooks_calling_another_installed_copy_are_left_alone(env, monkeypatch, tmp_path):
    other = app_wm(tmp_path / "Applications")  # a copy that stays: the one in use, or a test build
    ws = made_by(monkeypatch, other, "side work")
    monkeypatch.setenv(tmux.WM_ENV, str(app_wm(tmp_path / "Downloads")))
    assert ws_mod.repoint_hooks() == []
    assert hook_wm(ws) == {str(other)}


def test_hooks_calling_a_translocated_copy_are_repointed(env, monkeypatch, tmp_path):
    translocated = app_wm(tmp_path / "AppTranslocation" / "ABC123" / "d")
    ws = made_by(monkeypatch, translocated, "side work")
    here = app_wm(tmp_path / "Applications")
    monkeypatch.setenv(tmux.WM_ENV, str(here))
    assert ws_mod.repoint_hooks() == [ws.key]
    assert hook_wm(ws) == {str(here)}
