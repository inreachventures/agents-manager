import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentmgr import cli, config, db, plugins, templates
from agentmgr import workstream as ws_mod

FIXTURE = Path(__file__).parent / "fixtures" / "plugins" / "demo"
BASELINE = Path(__file__).parent / "data" / "claude_md_baseline.md"
PROGRESS = 'Append to "Decisions & progress"'
FACTS = 'Facts about this task (decisions, findings, state) go in TASK.md'


@pytest.fixture(autouse=True)
def fresh_plugins():
    plugins.reset()
    yield
    plugins.reset()


@pytest.fixture
def pdir(env, monkeypatch):
    d = env.root / "plugins"
    shutil.copytree(FIXTURE, d / "demo")
    monkeypatch.setenv("AGENTMGR_PLUGINS_DIR", str(d))
    monkeypatch.delenv("AGENTMGR_AUX", raising=False)
    config.get_config.cache_clear()
    return d


@pytest.fixture
def ws(pdir):
    return ws_mod.new(launch=False, name="x y", repo_queries=["web"])


def claude_md_of(ws) -> str:
    return (ws.path / "CLAUDE.md").read_text()


def regenerated(ws) -> str:
    plugins.reset()
    ws_mod.regenerate(db.connect(), db.get_workstream(db.connect(), ws.key))
    return claude_md_of(ws)


def test_claude_md_unchanged_without_plugins(tmp_path):
    def make(path):
        return db.Workstream(key="WS-1", ticket="PROJ-9", name="dark mode", session_id="s", folder=str(path),
                             status="x", status_reason=None, status_at=0, created_at=0, archived_at=None)

    links = [db.RepoLink("WS-1", "web", "/code/web", "/w/WS-1/web", "ws-1-dark", "main", True, 0)]
    intake = templates.claude_md(make("/nonexistent/WS-1"), links, ["web", "api"], Path("/mem"), ["web"], [], {})
    (tmp_path / "TASK.md").write_text("x")
    running = templates.claude_md(make(tmp_path), links, ["web", "api"], Path("/mem"), None, [], {})
    assert intake + "\n=====\n" + running == BASELINE.read_text()


def test_plugin_section_appended(ws):
    text = regenerated(ws)
    assert text.index("## Done means") < text.index("## Demo notes")
    assert "\n\n## Demo notes\n" in text and text.endswith("(none yet)\n")


def test_sections_in_plugin_name_order(pdir, env):
    shutil.copytree(FIXTURE, pdir / "aaa")
    (pdir / "aaa" / "plugin.toml").write_text((pdir / "aaa" / "plugin.toml").read_text().replace('"demo"', '"aaa"'))
    (pdir / "aaa" / "plugin.py").write_text(
        "def register(api):\n    api.claude_md(lambda ws: '## First')\n")
    ws = ws_mod.new(launch=False, name="x y", repo_queries=["web"])
    text = regenerated(ws)
    assert text.index("## First") < text.index("## Demo notes")


def test_replace_progress_rule(ws, monkeypatch):
    assert PROGRESS in regenerated(ws) and FACTS in claude_md_of(ws)
    monkeypatch.setenv("DEMO_REPLACE", "1")
    text = regenerated(ws)
    assert PROGRESS not in text and FACTS not in text
    assert "- Keep notes with `wm demo add`, not in TASK.md." in text


def test_replacement_conflict_first_wins(pdir, ws, monkeypatch):
    monkeypatch.setenv("DEMO_REPLACE", "1")
    shutil.copytree(FIXTURE, pdir / "zzz")
    (pdir / "zzz" / "plugin.toml").write_text((pdir / "zzz" / "plugin.toml").read_text().replace('"demo"', '"zzz"'))
    (pdir / "zzz" / "plugin.py").write_text(
        "def register(api):\n    api.replace_claude_md('progress_rule', lambda ws: '- zzz rule')\n")
    text = regenerated(ws)
    assert "- Keep notes with `wm demo add`" in text and "zzz rule" not in text
    assert [(e.name, e.what, e.reason) for e in plugins.errors()] == [
        ("zzz", "replace progress_rule", "already replaced by demo")]


def test_replace_intake_and_task_format(pdir, env):
    (pdir / "demo" / "plugin.py").write_text(
        "def register(api):\n    api.replace_claude_md('intake_summary', lambda ws: '4. Tell me.')\n"
        "    api.replace_claude_md('task_format', lambda ws: '## Mine')\n")
    ws = ws_mod.new(launch=False, name="x y", repo_queries=["web"])
    text = regenerated(ws)
    assert "4. Tell me." in text and "4. Summarise back" not in text
    assert "## Out of scope\n## Mine\n```" in text and "## Open questions" not in text


def test_allowed_tools_include_plugin(ws):
    cmd = ws_mod.claude_command(db.get_workstream(db.connect(), ws.key), resume=False)
    tools = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert f"Bash(wm demo add {ws.key} *)" in tools and f"Bash(wm set {ws.key} *)" in tools


def test_archive_calls_on_archive(ws, tmp_path, monkeypatch):
    marker = tmp_path / "marker"
    marker.mkdir()
    monkeypatch.setenv("DEMO_MARKER", str(marker))
    ws_mod.archive(ws.key, force=True)
    assert (marker / "archived.txt").read_text() == ws.key


def test_archive_survives_failing_on_archive(ws, tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MARKER", str(tmp_path / "missing"))  # writing there raises
    ws_mod.archive(ws.key, force=True)
    assert db.get_workstream(db.connect(), ws.key).status == db.ARCHIVED
    assert [e.what for e in plugins.errors()] == ["on_archive"]


def test_doctor_lists_plugin_problems(pdir, monkeypatch):
    monkeypatch.setenv("DEMO_DOCTOR", "1")
    (pdir / "broken").mkdir()
    (pdir / "broken" / "plugin.toml").write_text(
        'name = "broken"\ntitle = "B"\nversion = "1"\napi = 1\n')
    (pdir / "broken" / "plugin.py").write_text("raise RuntimeError('nope')\n")
    result = CliRunner().invoke(cli.app, ["doctor"])
    out = result.output
    assert "Plugins" in out
    assert "demo: example problem" in out
    assert "broken: skipped: RuntimeError: nope" in out


def test_doctor_without_plugins_has_no_plugins_heading(env, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTMGR_PLUGINS_DIR", str(tmp_path / "none"))
    config.get_config.cache_clear()
    assert "Plugins" not in CliRunner().invoke(cli.app, ["doctor"]).output


@pytest.mark.parametrize("register, what, reason", [
    ("api.claude_md(lambda ws: 42)", "claude_md", "returned int, not text"),
    ("api.claude_md(lambda ws: ['a'])", "claude_md", "returned list, not text"),
    ("api.replace_claude_md('intake_summary', lambda ws: 3)", "replace intake_summary", "returned int, not text"),
    ("api.allowed_tools(lambda ws: 7)", "allowed_tools", "returned int, not a list of text"),
    ("api.allowed_tools(lambda ws: 'Bash(x)')", "allowed_tools", "returned str, not a list of text"),
    ("api.allowed_tools(lambda ws: ['Bash(x)', 3])", "allowed_tools", "returned list, not a list of text"),
    ("api.doctor(lambda: 5)", "doctor", "returned int, not a list of text"),
])
def test_wrong_return_type_is_an_error_not_a_crash(env, monkeypatch, register, what, reason):
    d = env.root / "plugins"
    (d / "bad").mkdir(parents=True)
    (d / "bad" / "plugin.toml").write_text('name = "bad"\ntitle = "Bad"\nversion = "1"\napi = 1\n')
    (d / "bad" / "plugin.py").write_text(f"def register(api):\n    {register}\n")
    monkeypatch.setenv("AGENTMGR_PLUGINS_DIR", str(d))
    config.get_config.cache_clear()

    ws = ws_mod.new(launch=False, name="x y", repo_queries=["web"])
    cmd = ws_mod.claude_command(db.get_workstream(db.connect(), ws.key), resume=False)
    tools = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert f"Bash(wm set {ws.key} *)" in tools and "B" not in tools and "Bash(x)" not in tools
    assert "4. Summarise back" in claude_md_of(ws)
    result = CliRunner().invoke(cli.app, ["doctor"])
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    assert f"bad: {what}: {reason}" in result.output


def test_allowed_tool_with_a_comma_is_rejected(env, monkeypatch):
    d = env.root / "plugins"
    (d / "commas").mkdir(parents=True)
    (d / "commas" / "plugin.toml").write_text('name = "commas"\ntitle = "C"\nversion = "1"\napi = 1\n')
    (d / "commas" / "plugin.py").write_text(
        "def register(api):\n    api.allowed_tools(lambda ws: ['Bash(a, b)', 'Bash(ok *)'])\n")
    monkeypatch.setenv("AGENTMGR_PLUGINS_DIR", str(d))
    config.get_config.cache_clear()
    ws = ws_mod.new(launch=False, name="x y")
    cmd = ws_mod.claude_command(db.get_workstream(db.connect(), ws.key), resume=False)
    tools = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert "Bash(ok *)" in tools and not any(t.startswith("Bash(a") or t == " b)" for t in tools)
    assert [(e.name, e.what, e.reason) for e in plugins.errors()] == [
        ("commas", "allowed_tools", "'Bash(a, b)' has a comma, which --allowedTools can't pass on")]
