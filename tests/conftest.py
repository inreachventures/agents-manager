import subprocess
from pathlib import Path

import pytest

from agentmgr import config


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(root: Path, origins: Path, name: str) -> Path:
    """A bare 'origin' with one commit on main, cloned into the code root."""
    bare = origins / f"{name}.git"
    sh(origins, "git", "init", "-q", "--bare", "-b", "main", str(bare))
    seed = origins / f"{name}-seed"
    sh(origins, "git", "clone", "-q", str(bare), str(seed))
    (seed / "README.md").write_text(f"# {name}\n")
    sh(seed, "git", "add", ".")
    sh(seed, "git", "commit", "-q", "-m", "init")
    sh(seed, "git", "push", "-q", "origin", "HEAD:main")
    clone = root / name
    sh(root, "git", "clone", "-q", str(bare), str(clone))
    return clone


@pytest.fixture
def env(tmp_path, monkeypatch):
    code = tmp_path / "code"
    origins = tmp_path / "origins"
    code.mkdir()
    origins.mkdir()
    monkeypatch.setenv("AGENTMGR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENTMGR_CODE_ROOTS", str(code))
    monkeypatch.setenv("AGENTMGR_WORKSTREAMS_DIR", str(tmp_path / "workstreams"))
    monkeypatch.setenv("AGENTMGR_TMUX_SOCKET", f"agentmgr-test-{tmp_path.name}")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")
    config.get_config.cache_clear()
    for name in ("acme-web", "acme-ml-pipeline", "release-tools"):
        make_repo(code, origins, name)
    yield type("Env", (), {"code": code, "origins": origins, "root": tmp_path,
                           "workstreams": tmp_path / "workstreams"})
    config.get_config.cache_clear()
