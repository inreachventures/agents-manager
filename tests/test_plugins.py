import os
import re
import shutil
import sys
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentmgr import config, plugins
from agentmgr import workstream as ws_mod
from agentmgr.cli import app
from agentmgr.plugin import Badge, HookContext, HookResult, PluginError, RepoInfo, WorkstreamInfo

FIXTURE = Path(__file__).parent / "fixtures" / "plugins" / "demo"


@pytest.fixture(autouse=True)
def fresh_plugins():
    plugins.reset()
    yield
    plugins.reset()
    plugins.set_logger(plugins.stderr_logger)


@pytest.fixture
def logged():
    lines: list[str] = []
    plugins.set_logger(lines.append)
    return lines


@pytest.fixture
def pdir(env, monkeypatch):
    d = env.root / "plugins"
    monkeypatch.setenv("AGENTMGR_PLUGINS_DIR", str(d))
    config.get_config.cache_clear()
    return d


def install_demo(pdir):
    shutil.copytree(FIXTURE, pdir / "demo")


def make(pdir, folder, toml):
    (pdir / folder).mkdir(parents=True)
    (pdir / folder / "plugin.toml").write_text(toml)


def test_discover_reads_manifest(pdir):
    install_demo(pdir)
    manifests, skipped = plugins.discover()
    assert skipped == []
    (m,) = manifests
    assert (m.name, m.title, m.version, m.api, m.command, m.protect) == ("demo", "Demo", "0.1.0", 1, "demo", True)
    assert m.hooks == ("PostToolUse", "Stop")
    assert m.path == pdir / "demo"
    panel, main = m.views
    assert panel == plugins.ViewSpec(id="notes", title="Notes", place="panel", page="web/notes.html", shortcut=None)
    assert main.place == "main" and main.shortcut == "opt+cmd+d"


def test_missing_dir_is_empty(pdir):
    assert plugins.discover() == ([], [])


def test_bad_manifests_are_skipped(pdir):
    install_demo(pdir)
    make(pdir, "badtoml", "name = [")
    make(pdir, "noname", 'title = "x"\nversion = "1"\napi = 1\n')
    make(pdir, "wrongname", 'name = "other"\ntitle = "x"\nversion = "1"\napi = 1\n')
    make(pdir, "oldapi", 'name = "oldapi"\ntitle = "x"\nversion = "1"\napi = 0\n')
    make(pdir, "badplace", 'name = "badplace"\ntitle = "x"\nversion = "1"\napi = 1\n'
         '[[views]]\nid = "v"\ntitle = "V"\nplace = "side"\npage = "p.html"\n')
    make(pdir, "NotLower", 'name = "NotLower"\ntitle = "x"\nversion = "1"\napi = 1\n')
    manifests, skipped = plugins.discover()
    assert [m.name for m in manifests] == ["demo"]
    assert sorted(s.name for s in skipped) == ["NotLower", "badplace", "badtoml", "noname", "oldapi", "wrongname"]
    assert all(s.reason for s in skipped)
    assert {s.name: s.reason for s in skipped}["oldapi"] == "written for plugin API 0, wm has 1"


def test_unknown_hook_event_is_skipped(pdir):
    make(pdir, "hooky", 'name = "hooky"\ntitle = "x"\nversion = "1"\napi = 1\nhooks = ["PreToolUse", "Bogus"]\n')
    manifests, skipped = plugins.discover()
    assert manifests == []
    assert "Bogus" in skipped[0].reason


def view_toml(name, *views):
    head = f'name = "{name}"\ntitle = "x"\nversion = "1"\napi = 1\n'
    return head + "".join(f'[[views]]\nid = "{vid}"\ntitle = "V"\nplace = "main"\npage = "{page}"\n'
                          for vid, page in views)


def test_view_pages_must_stay_in_the_plugin_folder(pdir, tmp_path):
    make(pdir, "escape", view_toml("escape", ("v", "../../x.html")))
    make(pdir, "absolute", view_toml("absolute", ("v", str(tmp_path / "x.html"))))
    make(pdir, "linked", view_toml("linked", ("v", "web/x.html")))
    (pdir / "linked" / "web").symlink_to(tmp_path)  # a link out of the folder counts as outside
    make(pdir, "inside", view_toml("inside", ("v", "web/../index.html")))
    manifests, skipped = plugins.discover()
    assert [m.name for m in manifests] == ["inside"]
    assert {s.name: s.reason for s in skipped} == {
        "absolute": f"view page {str(tmp_path / 'x.html')!r} is outside the plugin folder",
        "escape": "view page '../../x.html' is outside the plugin folder",
        "linked": "view page 'web/x.html' is outside the plugin folder",
    }


def test_duplicate_view_ids_are_skipped(pdir):
    make(pdir, "twice", view_toml("twice", ("v", "a.html"), ("v", "b.html")))
    assert plugins.discover() == ([], [plugins.Skipped("twice", pdir / "twice", "view id 'v' is used twice")])


def test_forget_modules_while_other_threads_import():
    """The app-server loads plugins while other threads import: forgetting a plugin's modules must not trip over
    sys.modules changing size."""
    import threading
    import types

    stop = threading.Event()

    def churn():
        i = 0
        while not stop.is_set():
            name = f"_wm_test_churn_{i % 50}"
            sys.modules[name] = types.ModuleType(name)
            sys.modules.pop(f"_wm_test_churn_{(i + 25) % 50}", None)
            i += 1

    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    t = threading.Thread(target=churn)
    t.start()
    try:
        for _ in range(3000):
            plugins._forget_modules("wm_plugin_nothing")
    finally:
        stop.set()
        t.join()
        sys.setswitchinterval(interval)
        for name in [m for m in list(sys.modules) if m.startswith("_wm_test_churn_")]:
            del sys.modules[name]


def test_off_and_on(pdir, env):
    install_demo(pdir)
    cfg = config.get_config()
    cfg.config_file.write_text('code_roots = ["~/code"]\n')
    plugins.set_enabled("demo", False)
    manifests, skipped = plugins.discover()
    assert manifests == []
    assert [(s.name, s.reason) for s in skipped] == [("demo", "turned off")]
    assert 'code_roots = ["~/code"]' in cfg.config_file.read_text()
    plugins.set_enabled("demo", False)  # idempotent
    assert cfg.config_file.read_text().count('"demo"') == 1
    plugins.set_enabled("demo", True)
    manifests, skipped = plugins.discover()
    assert [m.name for m in manifests] == ["demo"] and skipped == []
    assert 'code_roots = ["~/code"]' in cfg.config_file.read_text()


def test_off_keeps_other_tables(pdir, env):
    cfg = config.get_config()
    cfg.config_file.write_text('[other]\nx = 1\n\n[plugins]\noff = ["a"]\nkeep = true\n\n[tail]\ny = 2\n')
    plugins.set_enabled("b", False)
    import tomllib

    data = tomllib.loads(cfg.config_file.read_text())
    assert data == {"other": {"x": 1}, "plugins": {"off": ["a", "b"], "keep": True}, "tail": {"y": 2}}


def test_fingerprint_changes(pdir):
    install_demo(pdir)
    first = plugins.fingerprint()
    assert plugins.fingerprint() == first
    py = pdir / "demo" / "plugin.py"
    st = py.stat()
    os.utime(py, (st.st_atime, st.st_mtime + 10))
    second = plugins.fingerprint()
    assert second != first
    plugins.set_enabled("demo", False)
    assert plugins.fingerprint() != second


def test_cli_plugins_lists_and_toggles(pdir):
    install_demo(pdir)
    runner = CliRunner()
    out = runner.invoke(app, ["plugins"])
    assert out.exit_code == 0 and "demo  on  0.1.0" in out.output
    assert runner.invoke(app, ["plugins", "off", "demo"]).exit_code == 0
    out = runner.invoke(app, ["plugins"])
    assert "demo  off" in out.output
    assert runner.invoke(app, ["plugins", "on", "demo"]).exit_code == 0
    assert "demo  on" in runner.invoke(app, ["plugins"]).output
    out = runner.invoke(app, ["plugins", "off", "x"])
    assert out.exit_code == 1 and "error: no plugin named 'x'" in out.output


def test_cli_plugins_shows_a_plugin_that_fails_to_import_as_skipped(pdir):
    install_demo(pdir)
    make(pdir, "broken", 'name = "broken"\ntitle = "B"\nversion = "1"\napi = 1\ncommand = "broken"\n')
    (pdir / "broken" / "plugin.py").write_text("import no_such_module_anywhere\n")
    out = CliRunner().invoke(app, ["plugins"]).output
    assert "broken  skipped  1  ModuleNotFoundError: No module named 'no_such_module_anywhere'" in out
    assert "demo  on  0.1.0" in out


def make_plugin(pdir, name, py, hooks=(), **files):
    hooks_toml = ", ".join(f'"{h}"' for h in hooks)
    make(pdir, name, f'name = "{name}"\ntitle = "{name.title()}"\nversion = "1"\napi = 1\nhooks = [{hooks_toml}]\n')
    (pdir / name / "plugin.py").write_text(py)
    for fname, text in files.items():
        (pdir / name / f"{fname}.py").write_text(text)


def only_demo():
    (lp,) = plugins.loaded()
    assert lp.manifest.name == "demo"
    return lp


def test_load_registers_everything(pdir):
    install_demo(pdir)
    lp = only_demo()
    r = lp.api.registered
    assert sorted(r.hooks) == ["PostToolUse", "Stop"]
    assert r.cli is not None
    assert sorted(r.requests) == ["add_note", "notes"]
    for fn in (r.claude_md, r.allowed_tools, r.badge, r.watch, r.on_archive, r.doctor):
        assert callable(fn)
    assert [secs for secs, _ in r.every] == [1]
    assert r.replace == {}
    assert plugins.skipped() == [] and plugins.errors() == []
    assert lp.api.name == "demo" and lp.api.aux_env() == {"AGENTMGR_AUX": "demo"}


def test_replace_registered_when_asked(pdir, monkeypatch):
    install_demo(pdir)
    monkeypatch.setenv("DEMO_REPLACE", "1")
    assert sorted(only_demo().api.registered.replace) == ["progress_rule"]


def test_loaded_once_per_process(pdir):
    install_demo(pdir)
    first = only_demo()
    assert plugins.loaded(command="demo") == [first]
    assert plugins.loaded(event="Stop") == [first]
    assert plugins.loaded(command="other") == []


def test_import_error_is_skipped(pdir):
    make_plugin(pdir, "broken", "import no_such_module_anywhere\n")
    assert plugins.loaded() == []
    (s,) = plugins.skipped()
    assert s.name == "broken"
    assert s.reason == "ModuleNotFoundError: No module named 'no_such_module_anywhere'"


def test_missing_register_is_skipped(pdir):
    make_plugin(pdir, "empty", "X = 1\n")
    assert plugins.loaded() == []
    assert "register" in plugins.skipped()[0].reason


def test_register_raises_is_skipped(pdir):
    install_demo(pdir)
    make_plugin(pdir, "angry", "def register(api):\n    raise RuntimeError('nope')\n")
    assert [lp.manifest.name for lp in plugins.loaded()] == ["demo"]
    assert [(s.name, s.reason) for s in plugins.skipped()] == [("angry", "RuntimeError: nope")]


def test_hook_for_undeclared_event_is_skipped(pdir):
    make_plugin(pdir, "sneaky", "def register(api):\n    api.hook('PreToolUse', lambda ctx: None)\n", hooks=["Stop"])
    assert plugins.loaded() == []
    (s,) = plugins.skipped()
    assert s.reason.startswith("ValueError: ") and "PreToolUse" in s.reason


def test_unknown_section_is_skipped(pdir):
    make_plugin(pdir, "replacer", "def register(api):\n    api.replace_claude_md('rules', lambda ws: 'x')\n")
    assert plugins.loaded() == []
    (s,) = plugins.skipped()
    assert s.reason.startswith("ValueError: ") and "'rules'" in s.reason


def test_cli_without_command_is_skipped(pdir):
    make_plugin(pdir, "nocmd", "import typer\ndef register(api):\n    api.cli(typer.Typer())\n")
    assert plugins.loaded() == []
    assert "command" in plugins.skipped()[0].reason


def test_bad_badge_tone():
    with pytest.raises(ValueError):
        Badge("3", tone="pink")
    assert Badge("3", tone="red").help == ""


def test_lazy_by_event(pdir):
    install_demo(pdir)
    sys.modules.pop("wm_plugin_demo", None)
    sys.modules.pop("wm_plugin_demo.plugin", None)
    assert plugins.loaded(event="PreToolUse") == []
    assert "wm_plugin_demo" not in sys.modules
    assert [lp.manifest.name for lp in plugins.loaded(event="PostToolUse")] == ["demo"]
    assert "wm_plugin_demo.plugin" in sys.modules


def fake_ws(tmp_path):
    return WorkstreamInfo(key="WS-9", name="x", ticket=None, folder=tmp_path / "WS-9", status="working", repos=())


def test_call_isolates_and_logs(pdir, monkeypatch, logged, tmp_path):
    install_demo(pdir)
    monkeypatch.setenv("DEMO_RAISE", "1")
    lp = only_demo()
    ctx = HookContext(ws=fake_ws(tmp_path), event="PostToolUse", payload={}, aux=False)
    assert plugins.call(lp, "hook PostToolUse", lp.api.registered.hooks["PostToolUse"], ctx, budget=2.0) is None
    assert logged == ["plugin demo: hook PostToolUse: RuntimeError: boom"]
    assert [(e.name, e.what, e.reason) for e in plugins.errors()] == [
        ("demo", "hook PostToolUse", "RuntimeError: boom")]
    assert plugins.skipped() == []
    assert plugins.loaded() == [lp]


def test_handler_error_cleared_by_success(pdir, logged):
    install_demo(pdir)
    lp = only_demo()

    def flaky(fail):
        if fail:
            raise ValueError("bad")
        return "ok"

    assert plugins.call(lp, "badge", flaky, True) is None
    assert plugins.call(lp, "doctor", flaky, True) is None
    assert [(e.what, e.reason) for e in plugins.errors()] == [("badge", "ValueError: bad"),
                                                              ("doctor", "ValueError: bad")]
    assert plugins.call(lp, "badge", flaky, False) == "ok"
    assert [e.what for e in plugins.errors()] == ["doctor"]
    assert plugins.call(lp, "doctor", flaky, False) == "ok"
    assert plugins.errors() == [] and plugins.skipped() == []


def test_skipped_includes_bad_manifests_not_turned_off(pdir):
    install_demo(pdir)
    make(pdir, "oldapi", 'name = "oldapi"\ntitle = "x"\nversion = "1"\napi = 2\n')
    make_plugin(pdir, "angry", "def register(api):\n    raise RuntimeError('nope')\n")
    make_plugin(pdir, "later", "def register(api): pass\n")
    plugins.set_enabled("later", False)
    assert [lp.manifest.name for lp in plugins.loaded()] == ["demo"]
    assert [(s.name, s.reason) for s in plugins.skipped()] == [
        ("angry", "RuntimeError: nope"), ("oldapi", "written for plugin API 2, wm has 1")]
    plugins.reset()
    assert [s.name for s in plugins.skipped()] == ["oldapi"]


def test_call_logs_over_budget(pdir, logged):
    install_demo(pdir)
    lp = only_demo()
    assert plugins.call(lp, "badge", lambda x: time.sleep(0.03) or x, 7, budget=0.01) == 7
    (line,) = logged
    assert re.fullmatch(r"plugin demo: badge took \d+\.\d\ds", line)
    assert plugins.call(lp, "badge", lambda: 8, budget=1.0) == 8
    assert len(logged) == 1


def test_sys_exit_is_contained(pdir, logged):
    make_plugin(pdir, "quitter", "import sys\ndef register(api):\n    sys.exit(2)\n")
    assert plugins.loaded() == []
    assert plugins.skipped()[0].reason == "SystemExit: 2"


def test_api_log(pdir, logged):
    install_demo(pdir)
    only_demo().api.log("hello")
    assert logged == ["plugin demo: hello"]


def test_two_plugins_sibling_modules(pdir):
    py = "from . import util\ndef register(api):\n    api.request('who', lambda: util.WHO)\n"
    make_plugin(pdir, "first-one", py, util="WHO = 'first'\n")
    make_plugin(pdir, "second", py, util="WHO = 'second'\n")
    a, b = plugins.loaded()
    assert (a.api.registered.requests["who"](), b.api.registered.requests["who"]()) == ("first", "second")
    assert "wm_plugin_first_one.util" in sys.modules and "wm_plugin_second.util" in sys.modules
    assert "util" not in sys.modules


def test_demo_fixture_behaviour(pdir, tmp_path, monkeypatch):
    install_demo(pdir)
    ws = ws_mod.new(launch=False, name="notes here", repo_queries=["web"])
    lp = only_demo()
    api, r = lp.api, lp.api.registered
    info = api.workstream(ws.key.lower())
    assert (info.key, info.name, info.folder) == (ws.key, "notes here", ws.path)
    (repo,) = info.repos
    assert isinstance(repo, RepoInfo) and repo.repo == "acme-web" and repo.worktree == ws.path / "acme-web"
    assert [w.key for w in api.workstreams()] == [ws.key]
    assert api.data_dir(info) == ws.path / ".wm" / "demo" and api.data_dir(info).is_dir()
    assert api.git(repo.worktree, "rev-parse", "--abbrev-ref", "HEAD") == repo.branch
    with pytest.raises(api.GitError):
        api.git(repo.worktree, "no-such-command")
    (repo.worktree / "new.txt").write_text("hi\n")
    assert "new.txt" in str(api.repo_diff(repo))
    assert isinstance(api.wm_executable(), str) and isinstance(api.claude_missing(), bool)
    assert api.claude_command() == config.get_config().claude_cmd
    with pytest.raises(PluginError):
        api.workstream("WS-404")
    with pytest.raises(PluginError, match="a workstream key is text, not int"):
        api.workstream(7)  # as a page's request might send it

    assert r.requests["notes"](key=ws.key) == []
    assert r.badge(info) is None
    r.requests["add_note"](key=ws.key, text="first")
    assert r.requests["notes"](key=ws.key) == ["first"]
    assert r.badge(info) == Badge(text="1", tone="blue", help="1 demo note")
    assert r.watch(info) == [api.data_dir(info) / "notes.json"]
    assert "## Demo notes" in r.claude_md(info) and "first" in r.claude_md(info)
    assert r.allowed_tools(info) == [f"Bash(wm demo add {ws.key} *)"]
    assert r.doctor() == []
    monkeypatch.setenv("DEMO_DOCTOR", "1")
    assert r.doctor() == ["demo: example problem"]

    ctx = HookContext(ws=info, event="PostToolUse", payload={}, aux=False)
    assert r.hooks["PostToolUse"](ctx) is None
    (api.data_dir(info) / "inbox.txt").write_text("look here")
    assert r.hooks["PostToolUse"](ctx) == HookResult(context="look here")
    assert not (api.data_dir(info) / "inbox.txt").exists()
    stop = HookContext(ws=info, event="Stop", payload={}, aux=False)
    assert r.hooks["Stop"](stop) is None
    (api.data_dir(info) / "job.txt").write_text("waiting on the build")
    assert r.hooks["Stop"](stop) == HookResult(waiting_on="waiting on the build")
    (api.data_dir(info) / "job.txt").unlink()
    (api.data_dir(info) / "block.txt").write_text("keep going")
    assert r.hooks["Stop"](stop) == HookResult(block="keep going")

    marker = tmp_path / "marker"
    marker.mkdir()
    monkeypatch.setenv("DEMO_MARKER", str(marker))
    r.on_archive(info)
    assert (marker / "archived.txt").read_text() == ws.key
    (_, tick), = r.every
    tick()
    tick()
    assert (marker / "ticks.txt").read_text() == "2"


def test_demo_cli_adds_note(pdir):
    install_demo(pdir)
    ws = ws_mod.new(launch=False, name="cli notes")
    lp = only_demo()
    out = CliRunner().invoke(lp.api.registered.cli, ["add", ws.key, "from the cli"])
    assert out.exit_code == 0, out.output
    assert lp.api.registered.requests["notes"](key=ws.key) == ["from the cli"]
    out = CliRunner().invoke(lp.api.registered.cli, ["add", "WS-404", "x"])
    assert out.exit_code != 0 and isinstance(out.exception, PluginError)


def test_archived_workstream_is_refused(pdir):
    install_demo(pdir)
    ws = ws_mod.new(launch=False, name="old one")
    ws_mod.archive(ws.key)
    api = only_demo().api
    assert api.workstreams() == []
    with pytest.raises(PluginError, match="archived"):
        api.workstream(ws.key)


@pytest.mark.parametrize("text", ['plugins = ["x"]\n', '[plugins]\noff = "demo"\n', '[plugins]\noff = ["demo", 3]\n'])
def test_malformed_off_is_read_leniently(pdir, text):
    install_demo(pdir)
    cfg = config.get_config()
    cfg.config_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.config_file.write_text(text)
    manifests, skipped = plugins.discover()
    turned_off = "demo" in text and "3" in text
    assert [m.name for m in manifests] == ([] if turned_off else ["demo"])
    assert [s.reason for s in skipped] == (["turned off"] if turned_off else [])


# Mounting plugin commands (cli.main runs in a subprocess: mounting happens there)


@pytest.fixture
def wm(env, pdir, monkeypatch):
    import subprocess

    mark = env.root / "import-mark"
    monkeypatch.setenv("DEMO_IMPORT_MARK", str(mark))

    def run(*args):
        exe = Path(sys.executable).parent / "wm"
        return subprocess.run([str(exe), *args], capture_output=True, text=True, env=os.environ.copy())

    run.mark = mark
    return run


def test_core_command_names_match_the_built_tree():
    import typer.main

    from agentmgr import cli

    assert cli._core_commands() == set(typer.main.get_command(cli.app).commands)


def test_plugin_command_is_mounted(wm, env, pdir):
    install_demo(pdir)
    ws = ws_mod.new(launch=False, name="cli notes")
    r = wm("demo", "add", ws.key, "hello")
    assert (r.returncode, r.stdout.strip()) == (0, "noted"), r.stderr
    assert "hello" in (env.workstreams / ws.key / ".wm" / "demo" / "notes.json").read_text()


def test_help_lists_plugin_commands_under_plugins(wm, pdir):
    install_demo(pdir)
    r = wm("--help")
    assert r.returncode == 0, r.stderr
    assert re.search(r"Plugins.*demo", r.stdout, re.S)
    assert wm.mark.exists()


def test_core_commands_do_not_import_plugins(wm, pdir):
    install_demo(pdir)
    for args in (["ls"], ["hook", "Stop"]):
        wm(*args)
        assert not wm.mark.exists(), args


def test_hook_imports_only_plugins_that_declare_the_event(wm, env, pdir):
    import json
    import subprocess

    install_demo(pdir)  # declares PostToolUse and Stop
    ws = ws_mod.new(launch=False, name="lazy hooks", repo_queries=["web"])
    payload = {"session_id": ws.session_id, "cwd": ws.folder, "tool_name": "Read",
               "tool_input": {"file_path": str(ws.path / "TASK.md")}}

    def hook(event):
        exe = Path(sys.executable).parent / "wm"
        r = subprocess.run([str(exe), "hook", event], input=json.dumps(payload), capture_output=True, text=True,
                           env=os.environ.copy())
        assert r.returncode == 0, r.stderr

    assert wm.mark.exists()  # creating the workstream imported it, in this process
    wm.mark.unlink()
    hook("PreToolUse")
    assert not wm.mark.exists()
    hook("PostToolUse")  # the same plugin, for an event it declares: imported
    assert wm.mark.exists()


def test_plugin_command_collision_with_core_is_skipped(wm, pdir):
    make(pdir, "bad", 'name = "bad"\ntitle = "Bad"\nversion = "1"\napi = 1\ncommand = "ls"\n')
    (pdir / "bad" / "plugin.py").write_text("def register(api):\n    api.cli(__import__('typer').Typer())\n")
    r = wm("ls")
    assert r.returncode == 0, r.stderr
    r = wm("--help")
    assert r.returncode == 0
    assert "command 'ls' is a core command" in r.stderr
    assert "command 'ls' is a core command" in wm("doctor").stdout


def test_plugin_error_prints_and_exits_1(wm, pdir):
    make(pdir, "bad", 'name = "bad"\ntitle = "Bad"\nversion = "1"\napi = 1\ncommand = "bad"\n')
    (pdir / "bad" / "plugin.py").write_text(
        "import typer\nfrom agentmgr.plugin import PluginError\nt = typer.Typer()\n\n\n@t.command()\n"
        "def go() -> None:\n    raise PluginError('nope')\n\n\ndef register(api):\n    api.cli(t)\n")
    r = wm("bad", "go")
    assert (r.returncode, r.stderr.strip()) == (1, "error: nope")


def test_command_of_a_plugin_that_fails_to_import_says_why(wm, pdir):
    make(pdir, "broken", 'name = "broken"\ntitle = "B"\nversion = "1"\napi = 1\ncommand = "broken"\n')
    (pdir / "broken" / "plugin.py").write_text("raise RuntimeError('nope')\n")
    r = wm("broken", "x")
    assert (r.returncode, r.stderr.strip()) == (1, "error: plugin broken is skipped: RuntimeError: nope")
    r = wm("nothing-declares-this")
    assert r.returncode == 2 and "No such command" in r.stderr


def test_broken_plugin_command_never_breaks_help(wm, pdir):
    install_demo(pdir)
    make(pdir, "broken", 'name = "broken"\ntitle = "B"\nversion = "1"\napi = 1\ncommand = "broken"\n')
    (pdir / "broken" / "plugin.py").write_text(
        "import typer\nfrom typer.models import CommandInfo\nt = typer.Typer()\n"
        "t.registered_commands.append(CommandInfo(name='x', callback=None))\n\n\n"
        "def register(api):\n    api.cli(t)\n")
    make(pdir, "notyper", 'name = "notyper"\ntitle = "N"\nversion = "1"\napi = 1\ncommand = "notyper"\n')
    (pdir / "notyper" / "plugin.py").write_text("def register(api):\n    api.cli(object())\n")
    r = wm("--help")
    assert r.returncode == 0, r.stderr
    assert "demo" in r.stdout and "broken: not mounted" in r.stderr and "notyper: not mounted" in r.stderr
    assert "broken: not mounted" in wm("doctor").stdout
    r = wm("broken", "x")
    assert (r.returncode, r.stderr.startswith("error: plugin broken")) == (1, True)
    assert "Traceback" not in r.stderr


def make_cli_plugin(pdir, name, command, says):
    make(pdir, name, f'name = "{name}"\ntitle = "T"\nversion = "1"\napi = 1\ncommand = "{command}"\n')
    (pdir / name / "plugin.py").write_text(
        "import typer\nt = typer.Typer()\n\n\n@t.command()\n"
        f"def hi() -> None:\n    typer.echo({says!r})\n\n\n@t.command()\ndef bye() -> None:\n    pass\n\n\n"
        "def register(api):\n    api.cli(t)\n")


def test_two_plugins_with_one_command_mount_the_first_by_name(wm, pdir):
    make_cli_plugin(pdir, "beta", "greet", "from beta")
    make_cli_plugin(pdir, "alpha", "greet", "from alpha")
    r = wm("greet", "hi")
    assert (r.returncode, r.stdout.strip()) == (0, "from alpha"), r.stderr
    r = wm("--help")
    assert r.returncode == 0, r.stderr
    assert "beta: not mounted: command 'greet' is also the alpha plugin's" in r.stderr
    assert "beta: not mounted: command 'greet' is also the alpha plugin's" in wm("doctor").stdout


def test_cli_plugins_shows_a_command_that_is_not_mounted(pdir):
    make_cli_plugin(pdir, "alpha", "greet", "from alpha")
    make_cli_plugin(pdir, "beta", "greet", "from beta")
    make_cli_plugin(pdir, "core", "ls", "from core")
    make(pdir, "notyper", 'name = "notyper"\ntitle = "N"\nversion = "1"\napi = 1\ncommand = "notyper"\n')
    (pdir / "notyper" / "plugin.py").write_text("def register(api):\n    api.cli(object())\n")
    out = CliRunner().invoke(app, ["plugins"]).output.splitlines()
    assert out == ["alpha  on  1",
                   "beta  on  1  not mounted: command 'greet' is also the alpha plugin's",
                   "core  on  1  not mounted: command 'ls' is a core command",
                   "notyper  on  1  not mounted: api.cli was given object, not a typer.Typer"]
