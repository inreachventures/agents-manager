import pytest

from agentmgr import tmux


def pane_pid(name):
    return tmux.run("display-message", "-p", "-t", f"={name}:", "#{pane_pid}").stdout.strip()


@pytest.mark.skipif(not tmux.available(), reason="needs tmux")
def test_reopening_home_restarts_a_dashboard_running_old_code(env, monkeypatch):
    try:
        tmux.ensure_home(["sleep", "600"])
        first = pane_pid("home")
        tmux.ensure_home(["sleep", "600"])
        assert pane_pid("home") == first  # same code: left running

        monkeypatch.setattr(tmux, "code_version", lambda: "newer")
        tmux.ensure_home(["sleep", "600"])
        second = pane_pid("home")
        assert second != first and tmux.session_option("home", "@wm_code") == "newer"

        monkeypatch.setattr(tmux, "code_version", lambda: "newest")
        monkeypatch.setattr(tmux, "clients", lambda name: ["/dev/ttys009"])  # shown in another terminal
        tmux.ensure_home(["sleep", "600"])
        assert pane_pid("home") == second and tmux.session_option("home", "@wm_code") == "newer"
    finally:
        tmux.run("kill-server", check=False)
