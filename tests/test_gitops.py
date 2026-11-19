import threading
from pathlib import Path

import pytest

from agentmgr import db, gitops

from .conftest import sh


def _st(wt):
    s = gitops.status(wt, "origin/main")
    return s.branch, s.dirty, s.ahead_of_base, s.behind_base, s.upstream, s.unpushed


def test_status_against_base_and_upstream(env):
    wt = env.code / "acme-web"
    sh(wt, "git", "switch", "-q", "-c", "feat")
    sh(wt, "git", "commit", "-q", "--allow-empty", "-m", "one")
    (wt / "new.txt").write_text("x")
    assert _st(wt) == ("feat", 1, 1, 0, None, 1)  # never pushed
    sh(wt, "git", "push", "-q", "-u", "origin", "feat")
    sh(wt, "git", "commit", "-q", "--allow-empty", "-m", "two")
    assert _st(wt) == ("feat", 1, 2, 0, "origin/feat", 1)
    sh(wt, "git", "push", "-q", "origin", ":feat")  # deleted after a merge: nothing counts as pushed any more
    sh(wt, "git", "fetch", "-q", "--prune")
    assert _st(wt) == ("feat", 1, 2, 0, None, 2)

    sh(wt, "git", "switch", "-q", "-c", "solo")
    sh(wt, "git", "push", "-q", "origin", "solo")  # without -u: origin/solo still counts as pushed
    sh(wt, "git", "commit", "-q", "--allow-empty", "-m", "three")
    assert _st(wt) == ("solo", 1, 3, 0, "origin/solo", 1)

    other = env.root / "other"
    sh(env.root, "git", "clone", "-q", str(env.origins / "acme-web.git"), str(other))
    sh(other, "git", "commit", "-q", "--allow-empty", "-m", "someone else")
    sh(other, "git", "push", "-q", "origin", "HEAD:main")
    sh(wt, "git", "fetch", "-q", "origin")
    sh(wt, "git", "switch", "-q", "--detach")
    assert _st(wt) == (None, 1, 3, 1, None, 1)  # one and two are on origin/solo

    # pushed under another branch name (a re-cut branch for the next PR): pushed all the same
    sh(wt, "git", "switch", "-q", "solo")
    sh(wt, "git", "push", "-q", "origin", "HEAD:solo-part-2")
    sh(wt, "git", "commit", "-q", "--allow-empty", "-m", "four")
    assert _st(wt) == ("solo", 1, 4, 1, "origin/solo", 1)
    assert gitops.status(wt, "origin/nope").ahead_of_base == 0  # unknown base: nothing counted


def test_git_dir_and_fingerprint_without_starting_git(env, monkeypatch):
    repo = env.code / "acme-web"
    wt = env.root / "wt"
    sh(repo, "git", "worktree", "add", "-q", "-b", "x", str(wt))
    (repo / "sub").mkdir()
    for path in (repo, wt, repo / "sub"):
        expected = Path(sh(path, "git", "rev-parse", "--absolute-git-dir")).resolve()
        assert gitops.git_dir(path).resolve() == expected

    refs = ["refs/heads/x", "refs/remotes/origin/main"]
    stamp = gitops.fingerprint(wt, refs)
    (wt / "edit.txt").write_text("x")
    assert gitops.fingerprint(wt, refs) == stamp  # only `git status` sees edits to the files themselves
    sh(wt, "git", "add", ".")
    assert gitops.fingerprint(wt, refs) != stamp  # staging rewrites the index
    stamp = gitops.fingerprint(wt, refs)
    sh(wt, "git", "commit", "-q", "-m", "x")
    assert gitops.fingerprint(wt, refs) != stamp
    stamp = gitops.fingerprint(wt, refs)
    sh(repo, "git", "update-ref", "refs/remotes/origin/main", "x")  # a fetch moving the base
    assert gitops.fingerprint(wt, refs) != stamp
    assert gitops.fingerprint(env.root / "missing", refs) is None

    def no_git(*args, **kwargs):
        raise AssertionError("started git")

    monkeypatch.setattr(gitops, "git", no_git)
    gitops.fingerprint(wt, refs)
    assert gitops.operation_in_progress(wt) is None


def test_one_database_connection_per_thread(env):
    conn = db.connect()
    assert db.connect() is conn
    theirs = []
    thread = threading.Thread(target=lambda: theirs.append(db.connect()))
    thread.start()
    thread.join()
    assert theirs[0] is not conn
    assert db.connect(env.root / "other.db") is not conn  # another database: the thread's old one is closed
    with pytest.raises(Exception, match="closed"):
        conn.execute("SELECT 1")


def test_not_landed_replays_only_the_commits_no_pr_carries(env):
    wt = env.code / "acme-web"
    sh(wt, "git", "switch", "-q", "-c", "feat")
    for name in ("feat.txt", "debug.sh"):
        (wt / name).write_text(name)
    sh(wt, "git", "add", ".")
    sh(wt, "git", "commit", "-q", "-m", "pr")
    pr_head = sh(wt, "git", "rev-parse", "HEAD")
    sh(wt, "git", "push", "-q", "origin", "HEAD:main")  # landed (a fast-forward stands in for the squash)
    sh(wt, "git", "fetch", "-q", "origin")
    assert gitops.not_landed(wt, "origin/main", [pr_head]) == 0
    sh(wt, "git", "rm", "-q", "debug.sh")
    sh(wt, "git", "commit", "-q", "-m", "drop debug")
    assert gitops.not_landed(wt, "origin/main", [pr_head]) == 1  # main still has debug.sh

    # a merge among them: counted as is, without replaying
    sh(wt, "git", "switch", "-q", "-c", "side", "HEAD~")
    sh(wt, "git", "commit", "-q", "--allow-empty", "-m", "side")
    sh(wt, "git", "switch", "-q", "feat")
    sh(wt, "git", "merge", "-q", "--no-edit", "side")
    assert gitops.not_landed(wt, "origin/main", [pr_head]) == 3


def test_not_landed_matches_a_copy_of_a_pr_commit_on_another_base(env):
    """The same change pushed for a PR from main, and kept locally on top of another PR's commit: carried by the PR."""
    wt = env.code / "acme-web"
    sh(wt, "git", "switch", "-q", "-c", "pr-1")
    (wt / "a.txt").write_text("a")
    sh(wt, "git", "add", ".")
    sh(wt, "git", "commit", "-q", "-m", "a")
    pr_1 = sh(wt, "git", "rev-parse", "HEAD")
    sh(wt, "git", "switch", "-q", "-c", "pr-2", "origin/main")
    (wt / "b.txt").write_text("b")
    sh(wt, "git", "add", ".")
    sh(wt, "git", "commit", "-q", "-m", "b")
    pr_2 = sh(wt, "git", "rev-parse", "HEAD")
    sh(wt, "git", "switch", "-q", "pr-1")
    sh(wt, "git", "cherry-pick", pr_2)  # b again, as a different commit on top of a
    assert gitops.not_landed(wt, "origin/main", [pr_1, pr_2]) == 0
    assert gitops.not_landed(wt, "origin/main", [pr_1]) == 1
    (wt / "c.txt").write_text("c")
    sh(wt, "git", "add", ".")
    sh(wt, "git", "commit", "-q", "-m", "c")
    assert gitops.not_landed(wt, "origin/main", [pr_1, pr_2]) == 1  # only c


def test_not_landed_matches_a_pr_squashed_locally(env):
    """A PR of several commits, kept locally as one commit making the same change: carried by the PR."""
    wt = env.code / "acme-web"
    sh(wt, "git", "switch", "-q", "-c", "pr")
    for name in ("a.txt", "b.txt"):
        (wt / name).write_text(name)
        sh(wt, "git", "add", ".")
        sh(wt, "git", "commit", "-q", "-m", name)
    pr_head = sh(wt, "git", "rev-parse", "HEAD")
    sh(wt, "git", "switch", "-q", "-c", "local", "origin/main")
    sh(wt, "git", "merge", "-q", "--squash", "pr")
    sh(wt, "git", "commit", "-q", "-m", "local mirror of the PR")
    assert gitops.not_landed(wt, "origin/main", [pr_head]) == 0
    assert gitops.not_landed(wt, "origin/main", []) == 1
    sh(wt, "git", "commit", "-q", "--allow-empty", "-m", "nothing")
    assert gitops.not_landed(wt, "origin/main", [pr_head]) == 0  # an empty commit changes nothing to lose
    (wt / "c.txt").write_text("c")
    sh(wt, "git", "add", ".")
    sh(wt, "git", "commit", "-q", "-m", "more")
    assert gitops.not_landed(wt, "origin/main", [pr_head]) == 3  # the whole run no longer matches
