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


def test_hooks_that_call_a_wm_that_is_gone_are_repointed(env, monkeypatch, tmp_path):
    def made_by(wm, name):
        monkeypatch.setenv(tmux.WM_ENV, str(wm))
        return ws_mod.new(launch=False, name=name)

    gone = fake_wm(tmp_path / "old-checkout" / ".venv" / "bin" / "wm")
    moved = made_by(gone, "moved work")
    on_image = made_by(app_wm(tmp_path / "Volumes" / "Workstreams"), "first work")
    installed = fake_wm(tmp_path / "opt" / "bin" / "wm")
    kept = made_by(installed, "other work")
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
