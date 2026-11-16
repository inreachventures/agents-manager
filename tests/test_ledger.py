import io
import json

import pytest
from typer.testing import CliRunner

from agentmgr import cli, db, guard, ledger, server, sidechat, transcript
from agentmgr import workstream as ws_mod


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("agentmgr.gh.available", lambda: False)
    monkeypatch.setattr("agentmgr.fetcher.fetch_stale", lambda *a, **k: {})
    monkeypatch.setattr("agentmgr.notify.notify", lambda *a: None)


def run_hook(monkeypatch, capsys, event, payload):
    from agentmgr import hooks

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks.main(event)
    out = capsys.readouterr().out
    return json.loads(out) if out.strip().startswith("{") else out


def remit(path):
    return ledger.ask(path, "How strict should the remit filter be?",
                      "It decides which companies reach the partners' Monday inbox.",
                      ["0.8 | about 1,200 a week", "0.6 | about 1,900 a week"], 1,
                      "I keep 0.8, today's setting")


def test_questions_need_options_and_a_default(tmp_path):
    with pytest.raises(ledger.LedgerError, match="two --option"):
        ledger.ask(tmp_path, "Q?", "", ["only one"], None, "wait")
    with pytest.raises(ledger.LedgerError, match="if-no-answer"):
        ledger.ask(tmp_path, "Q?", "", ["a", "b"], None, "  ")
    assert ledger.ask(tmp_path, "Log in to AWS", "", [], None, "I wait", kind=ledger.BLOCKER) == "Q1"
    assert remit(tmp_path) == "Q2"
    [_, q] = ledger.for_app(tmp_path)["questions"]
    assert [(o["label"], o["effect"], o["recommended"]) for o in q["options"]] == [
        ("0.8", "about 1,200 a week", True), ("0.6", "about 1,900 a week", False)]


def test_answers_wait_in_the_inbox_and_land_in_the_brief(tmp_path):
    qid = remit(tmp_path)
    assert ledger.answer(tmp_path, [{"id": qid, "option": 0}]) == ["Q1"]
    assert ledger.answer(tmp_path, [{"id": qid, "option": 1}]) == []  # already answered: ignored
    assert ledger.for_app(tmp_path)["questions"] == []
    assert ledger.inbox_size(tmp_path) == 1
    brief = (tmp_path / ".wm" / "BRIEF.md").read_text()
    assert "Q1: How strict should the remit filter be? → 0.8" in brief
    note = ledger.take_inbox(tmp_path)
    assert "Q1 (How strict should the remit filter be?) answered: 0.8" in note
    assert ledger.take_inbox(tmp_path) is None
    assert ledger.for_app(tmp_path)["changes"][0]["text"] == "You answered Q1: 0.8"


def test_revise_and_drop(tmp_path):
    qid = remit(tmp_path)
    ledger.revise(tmp_path, qid, title="How strict, given the digest only shows 30 days?")
    [q] = ledger.for_app(tmp_path)["questions"]
    assert q["revised"] and q["options"][0]["label"] == "0.8"
    ledger.drop(tmp_path, qid, "DIG's own flag is used instead")
    assert ledger.for_app(tmp_path)["questions"] == []
    assert "Dropped Q1" in ledger.for_app(tmp_path)["changes"][0]["text"]
    with pytest.raises(ledger.LedgerError, match="already dropped"):
        ledger.revise(tmp_path, qid, title="x")


def test_answering_an_assumption_makes_its_fact_the_users(tmp_path):
    qid = ledger.ask(tmp_path, "Does 7 out of 10 mean a strong founder?", "", ["Yes | 64 flagged", "No | type one"],
                     1, "I use 7", kind=ledger.ASSUMPTION)
    ledger.fact(tmp_path, "Strong founder = score of 7 or more", ledger.ASSUMED, question=qid)
    ledger.answer(tmp_path, [{"id": qid, "option": 0}])
    [f] = ledger.for_app(tmp_path)["facts"]
    assert f["source"] == ledger.YOU


def test_user_edits_are_recorded_and_queued(tmp_path):
    ledger.rule(tmp_path, "No automerge")
    assert ledger.user_edit(tmp_path, "rule", "r1", "No automerge on inreach repos") == (
        'Changed rule R1 from "No automerge" to "No automerge on inreach repos"')
    assert ledger.user_edit(tmp_path, "rule", None, "Budget $200") == "New rule R2: Budget $200"
    fid = ledger.fact(tmp_path, "Review rate is 77%")
    ledger.user_edit(tmp_path, "fact", fid, "Partners review everything we send")
    data = ledger.for_app(tmp_path)
    assert [r["text"] for r in data["rules"]] == ["No automerge on inreach repos", "Budget $200"]
    assert data["facts"][0]["source"] == ledger.YOU and data["inbox"] == 3
    ledger.user_edit(tmp_path, "rule", "R2", None, remove=True)
    assert [r["id"] for r in ledger.for_app(tmp_path)["rules"]] == ["R1"]
    with pytest.raises(ledger.LedgerError, match="no rule R9"):
        ledger.user_edit(tmp_path, "rule", "R9", "x")


def test_claude_md_imports_the_brief_and_preapproves_the_commands(env):
    ws = ws_mod.new(launch=False, name="ship it", repo_queries=["web"])
    text = (ws.path / "CLAUDE.md").read_text()
    assert "@.wm/BRIEF.md" in text and f'`wm ask {ws.key} "<question>"' in text
    tools = ws_mod.claude_command(ws, resume=False)
    allowed = tools[tools.index("--allowedTools") + 1]
    assert f"Bash(wm ask {ws.key} *)" in allowed and f"Bash(wm brief {ws.key})" in allowed


def test_claude_cannot_edit_the_ledger_directly(tmp_path):
    folder = tmp_path / "WS-1"
    ctx = guard.Context(ws_key="WS-1", folder=folder, cwd=folder, code_roots=[tmp_path / "code"], branches={})
    assert "is the brief" in guard.check_file_tool("Write", {"file_path": ".wm/ledger.json"}, ctx)


def test_cli_commands_write_the_ledger(env):
    ws = ws_mod.new(launch=False, name="ship it", repo_queries=["web"])
    run = CliRunner().invoke
    res = run(cli.app, ["ask", ws.key, "Merge #77?", "--context", "It turns on the new ranker",
                        "--option", "Yes | live for everyone", "--option", "No | stays off",
                        "--recommended", "2", "--if-no-answer", "I wait: merging can't be undone"])
    assert res.exit_code == 0, res.output
    assert "Q1 is in the brief" in res.output
    assert run(cli.app, ["fact", ws.key, "CI passes on #77", "--example", "run 123"]).exit_code == 0
    assert run(cli.app, ["rule", ws.key, "No automerge"]).exit_code == 0
    assert run(cli.app, ["state", ws.key, "PR #77 is open, CI running"]).exit_code == 0
    assert run(cli.app, ["step", ws.key, "Merge #77", "--waits-on", "Q1", "--waits-on", "job"]).exit_code == 0
    assert run(cli.app, ["step", ws.key, "Log in to AWS", "--owner", "you"]).exit_code == 0
    assert run(cli.app, ["step", ws.key, "--done", "S2"]).exit_code == 0
    assert run(cli.app, ["changed", ws.key, "CI failed on lint → fixed imports"]).exit_code == 0
    bad = run(cli.app, ["ask", ws.key, "--drop", "Q5", "--reason", "x"])
    assert bad.exit_code == 1 and "no question Q5" in bad.output
    out = run(cli.app, ["brief", ws.key]).output
    assert "R1: No automerge" in out and "F1 [found]: CI passes on #77" in out and "Merge #77?" in out
    data = ledger.for_app(ws.path, jobs_running=True)
    assert data["state"]["where"] == "PR #77 is open, CI running"
    merge, login = data["steps"]
    assert [b["id"] for b in merge["blocked_by"]] == ["Q1", "job"] and login["status"] == "done"
    assert "Done: Log in to AWS" in [c["text"] for c in data["changes"]]
    assert "S1 [todo, claude]: Merge #77 (waits on Q1, job)" in run(cli.app, ["brief", ws.key]).output


def test_hooks_hand_the_inbox_to_claude(env, monkeypatch, capsys):
    ws = ws_mod.new(launch=False, name="ship it", repo_queries=["web"])
    base = {"session_id": ws.session_id, "cwd": ws.folder}
    qid = remit(ws.path)

    run_hook(monkeypatch, capsys, "UserPromptSubmit", {**base, "prompt": "go"})
    assert ledger.load(ws.path)["turn"] == 1

    # answered while Claude works: handed over at the next tool call, as context rather than a chat message
    ledger.answer(ws.path, [{"id": qid, "option": 1}])
    out = run_hook(monkeypatch, capsys, "PostToolUse", {**base, "tool_name": "Read"})
    assert "answered: 0.6" in out["hookSpecificOutput"]["additionalContext"]

    # answered just as Claude stops: it carries on with the answer instead of ending the turn
    ledger.user_edit(ws.path, "rule", None, "Budget $200")
    out = run_hook(monkeypatch, capsys, "Stop", base)
    assert out["decision"] == "block" and "New rule R1: Budget $200" in out["reason"]
    assert db.get_workstream(db.connect(), ws.key).status == db.WORKING
    assert run_hook(monkeypatch, capsys, "Stop", base) == ""
    assert db.get_workstream(db.connect(), ws.key).status == db.YOUR_TURN

    # the next prompt picks up anything left
    ledger.user_edit(ws.path, "fact", None, "Partners review everything")
    out = run_hook(monkeypatch, capsys, "UserPromptSubmit", {**base, "prompt": "[wm] Answered in the brief: Q1"})
    assert "New fact F1" in out["hookSpecificOutput"]["additionalContext"]


def test_side_discussion_hooks_leave_the_main_session_alone(env, monkeypatch, capsys):
    ws = ws_mod.new(launch=False, name="ship it", repo_queries=["web"])
    db.set_status(db.connect(), ws.key, db.YOUR_TURN)
    ledger.user_edit(ws.path, "rule", None, "Budget $200")
    monkeypatch.setenv(sidechat.SIDE_ENV, "1")
    base = {"session_id": "fork-1", "cwd": ws.folder}
    assert run_hook(monkeypatch, capsys, "Stop", base) == ""
    run_hook(monkeypatch, capsys, "PostToolUse", {**base, "tool_name": "Read"})
    out = run_hook(monkeypatch, capsys, "PreToolUse", {**base, "tool_name": "Write",
                                                        "tool_input": {"file_path": "/etc/x"}})
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"  # the guardrails still apply
    assert db.get_workstream(db.connect(), ws.key).status == db.YOUR_TURN
    assert ledger.inbox_size(ws.path) == 1  # left for the main session


def ask_server(srv, method, **params):
    srv.out.seek(0)
    srv.out.truncate()
    srv.call(json.dumps({"id": 3, "method": method, "params": params}))
    return json.loads(srv.out.getvalue().splitlines()[0])


def test_server_brief_requests(env, monkeypatch):
    srv = server.Server(io.StringIO())
    ws = ws_mod.new(launch=False, name="ship it", repo_queries=["web"])
    qid = remit(ws.path)
    first = ask_server(srv, "brief", key=ws.key)["result"]
    assert [q["id"] for q in first["questions"]] == [qid]
    assert ask_server(srv, "brief", key=ws.key, since=first["digest"])["result"]["unchanged"]
    assert server.state({})["workstreams"][0]["open_questions"] == 1

    typed = []
    monkeypatch.setattr("agentmgr.tmux.send_text", lambda key, text: typed.append(text))
    monkeypatch.setattr("agentmgr.workstream.session_alive", lambda ws: True)
    monkeypatch.setattr("agentmgr.sessioncolor.vim_keys", lambda: False)
    screen = {"draft": ""}
    monkeypatch.setattr("agentmgr.tmux.capture",
                        lambda key: f"{'─' * 20}\n❯ {screen['draft']}\n{'─' * 20}\n  ? for shortcuts\n")
    db.set_status(db.connect(), ws.key, db.YOUR_TURN)
    res = ask_server(srv, "brief_answer", key=ws.key, answers=[{"id": qid, "option": 0}])["result"]
    assert res == {"answered": ["Q1"], "sent": True} and typed == ["[wm] Answered in the brief: Q1"]
    # something half-typed in the terminal: no line appended to it, and the chat view says why it can't send
    q2 = remit(ws.path)
    screen["draft"] = "actually, let me"
    res = ask_server(srv, "brief_answer", key=ws.key, answers=[{"id": q2, "option": 0}])["result"]
    assert res == {"answered": [q2], "sent": False} and len(typed) == 1
    assert "half-typed" in ask_server(srv, "send", key=ws.key, text="hi")["error"]
    screen["draft"] = ""
    ledger.take_inbox(ws.path)

    note = ask_server(srv, "brief_edit", key=ws.key, kind="rule", text="No automerge")["result"]["note"]
    assert note == "New rule R1: No automerge" and typed == ["[wm] Answered in the brief: Q1"]  # no turn for edits

    db.set_status(db.connect(), ws.key, db.WORKING)
    tid = ask_server(srv, "side_open", key=ws.key, quote="the 77% review rate")["result"]["id"]
    assert "working in the main session" in ask_server(srv, "side_ask", key=ws.key, id=tid, text="why?")["error"]

    db.set_status(db.connect(), ws.key, db.NEEDS_YOU)
    assert "prompt in the terminal" in ask_server(srv, "send", key=ws.key, text="hi")["error"]


def test_side_discussion_forks_then_resumes_the_fork(env, monkeypatch):
    ws = ws_mod.new(launch=False, name="ship it", repo_queries=["web"])
    qid = remit(ws.path)
    tid = ledger.open_thread(ws.path, {"question": qid})
    assert ledger.open_thread(ws.path, {"question": qid}) == tid  # one discussion per question
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw["env"].get(sidechat.SIDE_ENV)))
        return type("R", (), {"returncode": 0, "stderr": "",
                              "stdout": json.dumps({"result": "The digest shows 30 days.", "session_id": "fork-9"})})

    monkeypatch.setattr("agentmgr.sidechat.subprocess.run", fake_run)
    sidechat.ask(ws, tid, "doesn't the digest only show 30 days?")
    sidechat.ask(ws, tid, "then change it")
    (first, side1), (second, _) = calls
    assert side1 == "1"
    assert first[first.index("--resume") + 1] == ws.session_id and "--fork-session" in first
    assert "open question Q1" in first[first.index("-p") + 1]
    assert second[second.index("--resume") + 1] == "fork-9" and "--fork-session" not in second
    assert second[second.index("-p") + 1] == "then change it"
    t = ledger.thread(ws.path, tid)
    assert [m["role"] for m in t["messages"]] == ["user", "claude", "user", "claude"] and not t["busy"]


def test_transcript_reader_folds_tools_and_reads_only_new_lines(tmp_path):
    path = tmp_path / "s.jsonl"

    def write(*entries):
        with path.open("a") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    write({"type": "user", "uuid": "u1", "message": {"content": "Add a founder score"}},
          {"type": "assistant", "uuid": "a1", "message": {"content": [
              {"type": "tool_use", "name": "Read", "input": {"file_path": "/x/a.py"}},
              {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]}},
          {"type": "user", "uuid": "r1", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
          {"type": "assistant", "uuid": "a2", "message": {"content": [{"type": "text", "text": "Mostly wiring."}]}},
          {"type": "user", "uuid": "m1", "isMeta": True, "message": {"content": "<command-name>"}})
    reader = transcript.Reader(path)
    msgs = reader.read()
    assert [(m["role"], m["text"]) for m in msgs] == [
        ("user", "Add a founder score"), ("tools", "Read 1 file · ran 1 command"), ("claude", "Mostly wiring.")]
    assert msgs[1]["tools"] == ["Read: /x/a.py", "Bash: pytest -q"]
    offset = reader.offset
    write({"type": "user", "uuid": "u2", "message": {"content": "[wm] Answered in the brief: Q1"}})
    assert reader.read()[-1]["role"] == "wm" and reader.offset > offset


def entry(kind, content, **extra):
    return {"type": kind, "timestamp": "2026-11-13T10:00:00Z", "message": {"content": content}, **extra}


def write_transcript(path, *entries):
    with path.open("a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def test_jobs_run_until_their_notice_a_stop_or_their_timeout(tmp_path):
    path = tmp_path / "s.jsonl"
    write_transcript(
        path,
        entry("assistant", [{"type": "tool_use", "id": "t1", "name": "Bash",
                             "input": {"command": "python train.py", "description": "Train v5.1",
                                       "run_in_background": True}}]),
        entry("user", [{"type": "tool_result", "tool_use_id": "t1", "content": ""}],
              toolUseResult={"backgroundTaskId": "b1"}),
        entry("assistant", [{"type": "tool_use", "id": "t2", "name": "Monitor",
                             "input": {"command": "tail -F x.log", "description": "Fargate task logs",
                                       "timeout_ms": 3_600_000_000}}]),
        entry("user", [{"type": "tool_result", "tool_use_id": "t2", "content": ""}],
              toolUseResult={"taskId": "m1", "timeoutMs": 3_600_000_000, "persistent": False}))
    assert [j["text"] for j in transcript.running_jobs(path)] == ["Train v5.1", "Fargate task logs"]
    write_transcript(path, entry("user", "<task-notification> <task-id>b1</task-id> <status>completed</status> "
                                         "<summary>done</summary> </task-notification>"))
    assert [j["text"] for j in transcript.running_jobs(path)] == ["Fargate task logs"]
    write_transcript(path, entry("assistant", [{"type": "tool_use", "id": "t3", "name": "TaskStop",
                                                "input": {"task_id": "m1"}}]))
    assert transcript.running_jobs(path) == []
    # a notice is not a new turn in the chat
    assert all(m["role"] != "user" for m in transcript.Reader(path).read())


def test_waiting_on_a_job_is_not_your_turn(env, monkeypatch, capsys):
    ws = ws_mod.new(launch=False, name="ship it", repo_queries=["web"])
    path = tmp = ws.path / "t.jsonl"
    write_transcript(
        tmp,
        entry("user", "retrain it"),
        entry("assistant", [{"type": "tool_use", "id": "t1", "name": "Monitor",
                             "input": {"command": "tail -F x", "description": "Fargate retrain task",
                                       "persistent": True}}]),
        entry("user", [{"type": "tool_result", "tool_use_id": "t1", "content": ""}],
              toolUseResult={"taskId": "m1", "persistent": True}))
    db.set_status(db.connect(), ws.key, db.WORKING)
    notified = []
    monkeypatch.setattr("agentmgr.notify.notify", lambda *a: notified.append(a))
    run_hook(monkeypatch, capsys, "Stop", {"session_id": ws.session_id, "cwd": ws.folder,
                                           "transcript_path": str(path)})
    row = db.get_workstream(db.connect(), ws.key)
    assert row.status == db.YOUR_TURN and row.status_reason == "waiting on Fargate retrain task" and not notified
    wait = server.waiting(row, ledger.load(ws.path), transcript.running_jobs(path))
    assert wait == {"on": "job", "text": "waiting on Fargate retrain task"}
    # a question, or a step that is the user's, puts it back on the user
    ledger.step(ws.path, "Approve the PR", owner="you")
    assert server.waiting(row, ledger.load(ws.path), [])["on"] == "you"
    ledger.step(ws.path, status="done", update="S1")
    assert server.waiting(row, ledger.load(ws.path), []) == {"on": "done", "text": "all steps done"}


def test_stop_asks_once_for_the_state_after_real_work(env, monkeypatch, capsys):
    ws = ws_mod.new(launch=False, name="ship it", repo_queries=["web"])
    path = ws.path / "t.jsonl"
    tools = [{"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {}} for i in range(3)]
    write_transcript(path, entry("user", "go"), entry("assistant", tools))
    base = {"session_id": ws.session_id, "cwd": ws.folder, "transcript_path": str(path)}
    run_hook(monkeypatch, capsys, "UserPromptSubmit", {**base, "prompt": "go"})
    out = run_hook(monkeypatch, capsys, "Stop", base)
    assert out["decision"] == "block" and f"wm state {ws.key}" in out["reason"]
    assert run_hook(monkeypatch, capsys, "Stop", base) == ""  # once per turn
    run_hook(monkeypatch, capsys, "UserPromptSubmit", {**base, "prompt": "and now?"})
    ledger.state(ws.path, "PR open")
    assert run_hook(monkeypatch, capsys, "Stop", base) == ""  # already said where things stand


def test_the_plan_is_phases_in_order_with_decision_points(env):
    ws = ws_mod.new(launch=False, name="retrain", repo_queries=["web"])
    run = CliRunner().invoke
    for args in (["Retrain the model", "--owner", "job"], ["Evaluate: metrics and confidence intervals"],
                 ["Deploy v5.1"]):
        assert run(cli.app, ["phase", ws.key, *args]).exit_code == 0
    q = remit(ws.path)
    res = run(cli.app, ["phase", ws.key, "Decide whether to deploy", "--owner", "you", "--question", q,
                        "--before", "P3"])
    assert res.exit_code == 0 and "added P4" in res.output, res.output
    assert run(cli.app, ["phase", ws.key, "--start", "P1"]).exit_code == 0
    ledger.step(ws.path, "Watch the Fargate task")  # joins the current phase
    assert run(cli.app, ["phase", ws.key, "--start", "P2"]).exit_code == 0  # finishes P1
    phases = ledger.for_app(ws.path)["phases"]
    assert [p["id"] for p in phases] == ["P1", "P2", "P4", "P3"]
    assert [p["status"] for p in phases] == ["done", "doing", "todo", "todo"]
    assert phases[1]["current"] and phases[2]["question_open"] and phases[2]["owner"] == "you"
    assert ledger.load(ws.path)["steps"][0]["phase"] == "P1"
    ledger.answer(ws.path, [{"id": q, "option": 1}])
    assert ledger.for_app(ws.path)["phases"][2]["decided"] == "0.6"
    assert run(cli.app, ["phase", ws.key, "--skip", "P3"]).exit_code == 0
    brief = run(cli.app, ["brief", ws.key]).output
    assert "P4 [todo, you, decided by Q1]: Decide whether to deploy" in brief and "P3 [skipped" in brief
    assert "Started phase: Evaluate" in " ".join(c["text"] for c in ledger.load(ws.path)["changes"])
    from agentmgr import templates
    assert f"Bash(wm phase {ws.key} *)" in templates.allowed_tools(ws, ws.path)
    assert run(cli.app, ["phase", ws.key, "--start", "P9"]).exit_code == 1


def test_stop_trusts_claude_codes_list_of_running_work(env, monkeypatch, capsys):
    ws = ws_mod.new(launch=False, name="ship it", repo_queries=["web"])
    path = ws.path / "t.jsonl"
    write_transcript(path, entry("user", "go"))  # nothing in the transcript marks the job
    monkeypatch.setattr(transcript, "path_for", lambda *a: path)
    ledger.state(ws.path, "x")
    db.set_status(db.connect(), ws.key, db.WORKING)
    base = {"session_id": ws.session_id, "cwd": ws.folder, "transcript_path": str(path)}
    run_hook(monkeypatch, capsys, "Stop", {**base, "background_tasks": [
        {"id": "b7", "type": "shell", "status": "running", "description": "Run the evaluation notebook"}]})
    row = db.get_workstream(db.connect(), ws.key)
    assert row.status_reason == "waiting on Run the evaluation notebook"
    assert server._jobs(row)[0]["id"] == "b7"
    # the job's notice in the transcript ends it, even before the next stop
    with path.open("a") as f:
        f.write(json.dumps(entry("user", "<task-notification><task-id>b7</task-id><status>completed</status>"
                                 "</task-notification>")) + "\n")
    assert server._jobs(row) == []
    # an empty list at the next stop: nothing runs, so it's the user's turn
    run_hook(monkeypatch, capsys, "Stop", {**base, "background_tasks": [], "session_crons": []})
    row = db.get_workstream(db.connect(), ws.key)
    assert row.status == db.YOUR_TURN and row.status_reason is None
