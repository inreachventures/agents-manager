import asyncio

from textual.widgets import DataTable

from agentmgr import db, tmux, tui
from agentmgr import workstream as ws_mod


def test_archived_view_and_restore(env, monkeypatch):
    monkeypatch.setattr("agentmgr.gh.available", lambda: False)
    monkeypatch.setattr("agentmgr.fetcher.fetch_stale", lambda *a, **k: {})
    keep = ws_mod.new(launch=False, name="still active")
    gone = ws_mod.new(launch=False, name="old work", repo_queries=["web"])
    ws_mod.archive(gone.key)

    async def scenario():
        app = tui.Dashboard()
        async with app.run_test() as pilot:
            table = app.query_one(DataTable)

            async def shows(*keys):  # the table fills from worker threads; wait for it
                for _ in range(50):
                    await app.workers.wait_for_complete()
                    await pilot.pause(0.05)
                    if {r.value for r in table.rows} == set(keys):
                        return True
                return False

            assert await shows(keep.key)
            assert not app.check_action("unarchive", ())
            await pilot.press("v")
            assert await shows(gone.key)
            assert not app.check_action("archive", ()) and app.check_action("unarchive", ())
            await pilot.press("u")
            assert await shows(keep.key, gone.key)  # restored, back in the active view
            assert not app.show_archived

    asyncio.run(scenario())
    assert db.get_workstream(db.connect(), gone.key).status == db.STOPPED
    assert (gone.path / "acme-web").exists()


def test_ctrl_q_detaches_instead_of_quitting(env, monkeypatch):
    monkeypatch.setattr("agentmgr.gh.available", lambda: False)
    monkeypatch.setattr("agentmgr.fetcher.fetch_stale", lambda *a, **k: {})
    calls = []
    real_run = tmux.run
    monkeypatch.setattr("agentmgr.tmux.run", lambda *a, **k: calls.append(a) or real_run(*a, **k))

    async def scenario():
        app = tui.Dashboard()
        async with app.run_test() as pilot:
            await pilot.press("ctrl+q")
            await pilot.pause()
            assert app.is_running
        return calls

    assert ("detach-client",) in asyncio.run(scenario())


def test_rebase_from_the_dashboard(env, monkeypatch):
    from .conftest import sh

    monkeypatch.setattr("agentmgr.gh.available", lambda: False)
    monkeypatch.setattr("agentmgr.fetcher.fetch_stale", lambda *a, **k: {})
    ws = ws_mod.new(launch=False, ticket="PROJ-8", name="behind main", repo_queries=["web"])
    wt = ws.path / "acme-web"
    other = env.root / "other"
    sh(env.root, "git", "clone", "-q", str(env.origins / "acme-web.git"), str(other))
    (other / "b.txt").write_text("b")
    sh(other, "git", "add", ".")
    sh(other, "git", "commit", "-q", "-m", "main moved")
    sh(other, "git", "push", "-q", "origin", "HEAD:main")

    async def scenario():
        app = tui.Dashboard()
        async with app.run_test() as pilot:
            assert app.active_bindings["b"].binding.show  # listed in the footer
            for _ in range(50):
                await app.workers.wait_for_complete()
                await pilot.pause(0.05)
                if app.query_one(DataTable).row_count:
                    break
            await pilot.press("b")
            for _ in range(50):
                await app.workers.wait_for_complete()
                await pilot.pause(0.05)
                if isinstance(app.screen, tui.Confirm):
                    break
            assert "1 behind origin/main → rebase, not on origin yet" in str(app.screen.text)
            await pilot.press("y")
            for _ in range(50):
                await app.workers.wait_for_complete()
                await pilot.pause(0.05)
                if (wt / "b.txt").exists() and not isinstance(app.screen, tui.Busy):
                    break

    asyncio.run(scenario())
    assert (wt / "b.txt").exists()
