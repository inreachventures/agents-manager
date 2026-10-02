import pytest

from agentmgr import naming, repos
from agentmgr.config import get_config


def test_names_and_branches():
    assert naming.normalize_ticket(" proj-313 ") == "PROJ-313"
    with pytest.raises(naming.NamingError):
        naming.normalize_ticket("313")
    assert naming.normalize_name("Dark Mode Toggle!") == "dark mode toggle"
    with pytest.raises(naming.NamingError):
        naming.normalize_name("one two three four")
    assert naming.branch_name("WS-7", "PROJ-313", "dark mode toggle") == "proj-313-dark-mode-toggle"
    assert naming.branch_name("WS-7", None, "quick fix") == "ws-7-quick-fix"
    assert naming.branch_carries_id("feat/PROJ-313-x", "WS-7", "PROJ-313")
    assert not naming.branch_carries_id("main", "WS-7", "PROJ-313")
    assert naming.symlink_name("WS-7", "PROJ-313", "a b") == "PROJ-313-a-b"
    assert naming.symlink_name("WS-7", None, None) is None


def test_repo_matching(env):
    found = repos.discover(get_config())
    assert [r.name for r in found] == ["acme-ml-pipeline", "acme-web", "release-tools"]
    assert repos.match("acme ML pipeline", found).name == "acme-ml-pipeline"
    assert repos.match("web", found).name == "acme-web"
    assert repos.match("release toolz", found).name == "release-tools"
    with pytest.raises(repos.RepoMatchError, match="Candidates"):
        repos.match("acme", found)
