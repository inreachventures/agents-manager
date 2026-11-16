import io
import json
import os
import time
import uuid
from pathlib import Path

import pytest

from agentmgr import db, server, sessioncolor, tmux, workstream
from agentmgr import workstream as ws_mod

# The prompt area of real Claude Code 2.1.325 screens (tmux capture-pane -e), with a past prompt above it
REAL = json.loads((Path(__file__).parent / "data" / "claude-prompts.json").read_text(encoding="utf-8"))
E_ = "\x1b"
RULE = f"{E_}[38;5;244m" + "─" * 40 + f"{E_}[39m"


def screen(prompt: str, mode: str = "-- INSERT --") -> str:
    return "\n".join([RULE, f"{E_}[39m❯\xa0{prompt}", RULE, f"  {E_}[38;5;246m{mode}{E_}[39m ⏵⏵ auto mode on"])


def test_prompt_detection_on_real_claude_screens():
    text = {name: sessioncolor.prompt_text(s) for name, s in REAL.items()}
    draft = "fix the flaky test"
    assert text == {"empty": "", "empty-normal": "", "draft": draft, "draft-normal": draft, "multiline": None,
                    "typed-command": "/color green"}
    ready = {name for name, s in REAL.items() if sessioncolor.prompt_ready(s, vim=True)}
    assert ready == {"empty"}  # not in vim normal mode, nor over a draft of one line or several
    assert {name for name, s in REAL.items() if sessioncolor.prompt_ready(s, vim=False)} == {"empty", "empty-normal"}


def test_only_a_dim_run_that_ends_counts_as_the_placeholder():
    assert sessioncolor.prompt_text(screen(f"{E_}[2mTry this{E_}[0m")) == ""
    assert sessioncolor.prompt_text(screen(f"{E_}[2mchip{E_}[0;1mreal text")) == "real text"  # combined reset
    assert sessioncolor.prompt_text(screen(f"{E_}[2mno reset at all")) == "no reset at all"
    assert sessioncolor.prompt_text("Allow Bash?\n❯ 1. Yes\n  2. No") is None  # a permission prompt's choice
    assert sessioncolor.prompt_text("") is None


def test_vim_keys_when_set_or_unreadable(env):
    home = Path.home()
    assert not sessioncolor.vim_keys()
    (home / ".claude.json").write_text('{"editorMode": "vim"}')
    assert sessioncolor.vim_keys()
    (home / ".claude.json").write_text('{"editorMode": "nor')  # caught mid-write
    assert sessioncolor.vim_keys()
    (home / ".claude.json").write_text("{}")
    assert not sessioncolor.vim_keys()


def transcript(ws, name: str, *entries: dict):
    folder = sessioncolor.transcripts_dir(ws)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.jsonl"
    with path.open("a") as f:
        f.writelines(json.dumps(e) + "\n" for e in entries)
    return path


def color(c: str) -> dict:
    return {"type": "agent-color", "agentColor": c, "sessionId": "s"}


def test_current_color_is_the_last_one_in_the_newest_transcript(env):
    ws = ws_mod.new(launch=False, name="colours")
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    assert sessioncolor.current(ws) is None  # no session yet
    first = transcript(ws, a, {"type": "user"}, color("red"))
    assert sessioncolor.current(ws) == "red"
    transcript(ws, a, {"type": "user"}, color("pink"))
    assert sessioncolor.current(ws) == "pink"  # appended: read from where it left off
    transcript(ws, a, color("default"))
    assert sessioncolor.current(ws) is None
    with first.open("a") as f:
        f.write('{"type":"agent-color","agentColor":"blue"')  # still being written
    assert sessioncolor.current(ws) is None
    with first.open("a") as f:
        f.write(',"sessionId":"s"}\n')
    assert sessioncolor.current(ws) == "blue"
    later = time.time() + 5
    os.utime(transcript(ws, "agent-1234", color("green")), (later, later))  # not a session's transcript
    assert sessioncolor.current(ws) == "blue"
    os.utime(transcript(ws, b, {"type": "user"}), (later, later))  # /clear: a new transcript, no colour
    assert sessioncolor.current(ws) is None


class FakeSession:
    """A Claude prompt in a tmux pane: what wm types lands in it, Enter sends it."""

    def __init__(self, monkeypatch, prompt=""):
        self.prompt, self.sent, self.typing_too = prompt, [], ""
        monkeypatch.setattr(workstream, "session_alive", lambda w: True)
        monkeypatch.setattr(tmux, "capture", lambda name: screen(self.prompt))
        monkeypatch.setattr(tmux, "type_text", self.type)
        monkeypatch.setattr(tmux, "press_enter", self.enter)
        monkeypatch.setattr(sessioncolor, "vim_keys", lambda: True)
        monkeypatch.setattr(time, "sleep", lambda s: None)

    def type(self, name, text):
        self.prompt += text + self.typing_too  # what you type meanwhile lands after it

    def enter(self, name):
        self.sent.append(self.prompt)
        self.prompt = ""


def test_a_picked_colour_is_typed_once_claude_waits_at_an_empty_prompt(env, monkeypatch):
    ws = ws_mod.new(launch=False, name="colours")
    conn = db.connect()
    pane = FakeSession(monkeypatch, prompt="half typed")
    srv = server.Server(io.StringIO())
    row_color = lambda: server.state(srv.chosen_colors())["workstreams"][0]["color"]  # noqa: E731

    db.set_status(conn, ws.key, db.WORKING)
    assert srv.rpc_set_color(ws.key, "green") == {"applied": False}  # busy: wait
    assert row_color() == "green" and pane.sent == []
    db.set_status(conn, ws.key, db.YOUR_TURN)
    srv.apply_colors()
    assert pane.sent == []  # a message started at the prompt: don't type into it
    pane.prompt = ""
    srv.apply_colors()
    srv.apply_colors()
    assert pane.sent == ["/color green"]  # once
    assert row_color() == "green"  # until Claude's transcript has it
    transcript(ws, str(uuid.uuid4()), color("green"))
    srv.apply_colors()
    assert srv.colors == {} and row_color() == "green"

    assert srv.rpc_set_color(ws.key, "default") == {"applied": True}
    assert pane.sent[-1] == "/color default" and row_color() is None


def test_the_last_pick_wins_and_typing_over_it_cancels_it(env, monkeypatch):
    ws = ws_mod.new(launch=False, name="colours")
    conn = db.connect()
    pane = FakeSession(monkeypatch)
    out = io.StringIO()
    srv = server.Server(out)
    db.set_status(conn, ws.key, db.WORKING)
    srv.rpc_set_color(ws.key, "green")
    srv.rpc_set_color(ws.key, "pink")  # changed your mind while Claude works
    db.set_status(conn, ws.key, db.YOUR_TURN)
    srv.apply_colors()
    assert pane.sent == ["/color pink"]

    pane.typing_too = "fix"  # you start typing just as wm types the command
    srv.rpc_set_color(ws.key, "blue")
    assert pane.sent == ["/color pink"] and pane.prompt == "/color bluefix"  # not sent: left for you to see
    assert srv.colors == {} and "didn't send" in out.getvalue()

    with pytest.raises(workstream.WorkstreamError):
        srv.rpc_set_color(ws.key, "mauve")
