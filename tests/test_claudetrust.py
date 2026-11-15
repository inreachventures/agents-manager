import json
import os
from pathlib import Path

from agentmgr import claudetrust


def _write(path: Path, data) -> Path:
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def test_untrusted_folder_gets_trusted(tmp_path):
    state = _write(tmp_path / ".claude.json", {
        "projects": {"/x/WS-1": {"hasTrustDialogAccepted": True, "foo": 1}}, "other": 2})
    assert claudetrust.ensure_trusted(Path("/x"), state) is True
    data = json.loads(state.read_text())
    assert data["projects"]["/x"]["hasTrustDialogAccepted"] is True
    assert data["projects"]["/x/WS-1"] == {"hasTrustDialogAccepted": True, "foo": 1}
    assert data["other"] == 2


def test_existing_entry_keeps_its_keys(tmp_path):
    state = _write(tmp_path / ".claude.json", {"projects": {"/x": {"allowedTools": ["a"]}}})
    assert claudetrust.ensure_trusted(Path("/x"), state) is True
    assert json.loads(state.read_text())["projects"]["/x"] == {
        "allowedTools": ["a"], "hasTrustDialogAccepted": True}


def _unchanged(state: Path, folder: Path) -> bool:
    before, mtime = state.read_bytes(), state.stat().st_mtime_ns
    wrote = claudetrust.ensure_trusted(folder, state)
    return not wrote and state.read_bytes() == before and state.stat().st_mtime_ns == mtime


def test_trusted_ancestor_is_left_alone(tmp_path):
    state = _write(tmp_path / ".claude.json",
                   {"projects": {str(tmp_path.resolve()): {"hasTrustDialogAccepted": True}}})
    assert _unchanged(state, tmp_path / "ws")


def test_already_trusted_is_left_alone(tmp_path):
    folder = tmp_path / "ws"
    state = _write(tmp_path / ".claude.json",
                   {"projects": {str(folder.resolve()): {"hasTrustDialogAccepted": True}}})
    assert _unchanged(state, folder)


def test_missing_file_is_a_noop(tmp_path):
    state = tmp_path / ".claude.json"
    assert claudetrust.ensure_trusted(tmp_path, state) is False
    assert not state.exists()


def test_invalid_json_is_a_noop(tmp_path):
    state = _write(tmp_path / ".claude.json", "{not json")
    assert claudetrust.ensure_trusted(tmp_path, state) is False
    assert state.read_text() == "{not json"


def test_non_dict_top_level_is_a_noop(tmp_path):
    state = _write(tmp_path / ".claude.json", "[1, 2]")
    assert claudetrust.ensure_trusted(tmp_path, state) is False
    assert state.read_text() == "[1, 2]"


def test_file_mode_preserved(tmp_path):
    state = _write(tmp_path / ".claude.json", {})
    state.chmod(0o600)
    assert claudetrust.ensure_trusted(tmp_path, state) is True
    assert state.stat().st_mode & 0o777 == 0o600
    assert os.listdir(tmp_path) == [".claude.json"]  # no temp file left behind


def test_is_trusted(tmp_path):
    folder = tmp_path / "ws"
    state = tmp_path / ".claude.json"
    assert claudetrust.is_trusted(folder, state) is False  # missing file
    _write(state, {"projects": {str(folder.resolve()): {"hasTrustDialogAccepted": True}}})
    assert claudetrust.is_trusted(folder, state) is True
    assert claudetrust.is_trusted(folder / "sub", state) is True  # ancestor
    assert claudetrust.is_trusted(tmp_path / "other", state) is False


def test_env_home_is_isolated(env):
    assert Path.home() == env.root / "userhome"


def test_non_dict_projects_is_left_alone(tmp_path):
    state = _write(tmp_path / ".claude.json", {"projects": ["x"], "other": 2})
    assert _unchanged(state, tmp_path / "ws")


def test_non_dict_entry_is_left_alone(tmp_path):
    folder = tmp_path / "ws"
    state = _write(tmp_path / ".claude.json", {"projects": {str(folder.resolve()): "x"}})
    assert _unchanged(state, folder)


def test_symlinked_state_file_stays_a_symlink(tmp_path):
    real = _write(tmp_path / "real.json", {"projects": {}})
    state = tmp_path / ".claude.json"
    state.symlink_to(real)
    folder = tmp_path / "ws"
    assert claudetrust.ensure_trusted(folder, state) is True
    assert state.is_symlink()
    assert json.loads(real.read_text())["projects"][str(folder.resolve())] == {
        "hasTrustDialogAccepted": True}
