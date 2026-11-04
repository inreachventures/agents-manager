import io
import json
import tomllib

from agentmgr import config, guard, repos, server
from agentmgr import workstream as ws_mod

from .conftest import make_repo, sh


def single_repo(env, name="side-project"):
    elsewhere = env.root / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    return make_repo(elsewhere, env.origins, name)


def test_save_paths_rewrites_only_its_keys(env, monkeypatch):
    monkeypatch.delenv("AGENTMGR_CODE_ROOTS")
    cfg_file = config.get_config().config_file
    cfg_file.write_text('# my settings\nclaude_cmd = "claude"  # keep me\ncode_roots = [\n  "~/old",\n]\n\n'
                        '[extra]\nrepos = ["not ours"]\n')
    side = single_repo(env)
    config.save_paths([env.code], [side])
    text = cfg_file.read_text()
    assert "# my settings" in text and 'claude_cmd = "claude"  # keep me' in text and "~/old" not in text
    data = tomllib.loads(text)
    assert data["extra"] == {"repos": ["not ours"]}  # a table's own keys are left alone
    cfg = config.get_config()
    assert cfg.code_roots == (env.code,) and cfg.repos == (side,) and cfg.checkouts == (env.code, side)


def test_single_repos_are_found_and_guarded_like_the_code_roots(env, monkeypatch):
    monkeypatch.delenv("AGENTMGR_CODE_ROOTS")
    side = single_repo(env)
    sh(env.root, "git", "init", "-q", str(env.root / "elsewhere" / "acme-web"))  # same name as one in the code root
    config.save_paths([env.code], [side, env.root / "elsewhere" / "acme-web"])
    found = {r.name: r.path for r in repos.discover(config.get_config())}
    assert found["side-project"] == side and found["acme-web"] == env.code / "acme-web"  # the code root's wins

    ws = ws_mod.new(launch=False, name="side work", repo_queries=["side-project"])
    settings = json.loads((ws.path / ".claude" / "settings.json").read_text())
    assert f"Edit(/{side}/**)" in settings["permissions"]["deny"]
    ctx = guard.Context(ws_key=ws.key, folder=ws.path, cwd=ws.path, code_roots=[env.code],
                        branches={"side-project": "ws-1-side-work"}, repos=[side])
    reason = guard.check_bash(f"cd {side}/src && git commit -am x", ctx)
    assert reason and f"use {ws.path / 'side-project' / 'src'}" in reason
    other = guard.check_bash(f"git -C {env.root / 'elsewhere' / 'acme-web'} commit -m x", ctx)
    assert other is None  # outside the checkouts this guard was given


def test_setup_requests(env, monkeypatch):
    monkeypatch.delenv("AGENTMGR_CODE_ROOTS")
    monkeypatch.setattr("agentmgr.gh.available", lambda: False)
    srv = server.Server(io.StringIO())

    def ask(method, **params):
        srv.out.seek(0)
        srv.out.truncate()
        srv.call(json.dumps({"id": 1, "method": method, "params": params}))
        return json.loads(srv.out.getvalue().splitlines()[0])

    side = single_repo(env)
    preview = ask("scan", code_roots=[str(env.code)], repos=[str(side), str(env.root / "nope")])["result"]
    assert "side-project" in preview["found"] and preview["problems"] == [
        f"{env.root / 'nope'} is not a git repository (no .git folder)"]
    assert "not a git repository" in ask("save_setup", code_roots=[str(env.code)], repos=[str(env.root)])["error"]

    ws = ws_mod.new(launch=False)  # its CLAUDE.md lists the repos it can attach
    assert "side-project" not in (ws.path / "CLAUDE.md").read_text()
    saved = ask("save_setup", code_roots=[str(env.code)], repos=[str(side)])["result"]
    assert saved["repos"] == [str(side)] and "side-project" in saved["found"]
    assert "side-project" in (ws.path / "CLAUDE.md").read_text()
    assert set(saved["tools"]) == {"tmux", "claude", "git", "gh"}
