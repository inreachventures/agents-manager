import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentmgr import config, plugins
from agentmgr.cli import app

FIXTURE = Path(__file__).parent / "fixtures" / "plugins" / "demo"


@pytest.fixture
def pdir(env, monkeypatch):
    d = env.root / "plugins"
    monkeypatch.setenv("AGENTMGR_PLUGINS_DIR", str(d))
    config.get_config.cache_clear()
    return d


def install_demo(pdir):
    shutil.copytree(FIXTURE, pdir / "demo")
    (pdir / "demo" / "plugin.py").write_text("def register(api): pass\n")


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
