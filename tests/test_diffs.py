import os

from agentmgr import diffs
from agentmgr import workstream as ws_mod

from .conftest import sh

PATCH = """\
diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1,3 +1,3 @@ def main():
 one
-two
+deux
 three
@@ -10,2 +10,3 @@
 ten
+ten and a half
 eleven
\\ No newline at end of file
diff --git a/logo.png b/logo.png
new file mode 100644
index 0000000..3333333
Binary files /dev/null and b/logo.png differ
diff --git a/old name.txt b/new name.txt
similarity index 90%
rename from old name.txt
rename to new name.txt
--- a/old name.txt
+++ b/new name.txt
@@ -1 +1 @@
-hello
+hello there
diff --git a/gone.txt b/gone.txt
deleted file mode 100644
--- a/gone.txt
+++ /dev/null
@@ -1 +0,0 @@
-bye
"""


def test_parse_numbers_lines_and_reads_file_headers():
    app, logo, renamed, gone = diffs.parse(PATCH)
    assert (app.path, app.status, app.additions, app.deletions) == ("app.py", "modified", 2, 1)
    first, second = app.hunks
    assert [(ln.kind, ln.old, ln.new, ln.text) for ln in first.lines] == [
        (" ", 1, 1, "one"), ("-", 2, None, "two"), ("+", None, 2, "deux"), (" ", 3, 3, "three")]
    assert [(ln.old, ln.new) for ln in second.lines] == [(10, 10), (None, 11), (11, 12)]
    assert (logo.path, logo.status, logo.binary, logo.hunks) == ("logo.png", "added", True, [])
    assert (renamed.path, renamed.old_path, renamed.status) == ("new name.txt", "old name.txt", "renamed")
    assert (gone.path, gone.status, gone.deletions) == ("gone.txt", "deleted", 1)


def test_parse_truncates_but_keeps_counting(monkeypatch):
    monkeypatch.setattr(diffs, "MAX_FILE_LINES", 2)
    [f] = diffs.parse("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -0,0 +1,3 @@\n+a\n+b\n+c\n")
    assert f.truncated and f.additions == 3 and [ln.text for ln in f.hunks[0].lines] == ["a", "b"]
    [f, g] = diffs.parse("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -0,0 +1,1 @@\n+a\n"
                         "diff --git a/y b/y\n--- a/y\n+++ b/y\n@@ -0,0 +1,1 @@\n+b\n", budget=1)
    assert not f.truncated and g.truncated and g.additions == 1 and g.hunks == []


def test_repo_diff_covers_commits_staged_unstaged_and_untracked_files(env):
    ws = ws_mod.new(launch=False, name="show changes", repo_queries=["web"])
    wt = ws.path / "acme-web"
    (wt / "committed.txt").write_text("c\n")
    sh(wt, "git", "add", ".")
    sh(wt, "git", "commit", "-q", "-m", "work")
    (wt / "README.md").write_text("# acme-web\nedited\n")
    (wt / "staged.txt").write_text("s\n")
    sh(wt, "git", "add", "staged.txt")
    (wt / "untracked.txt").write_text("u1\nu2\n")
    (wt / "blob.bin").write_bytes(b"\0\1\2")
    before = sh(wt, "git", "status", "--porcelain")

    out = diffs.repo_diff(wt, "origin/main")
    files = {f["path"]: f for f in out["files"]}
    assert set(files) == {"committed.txt", "README.md", "staged.txt", "untracked.txt", "blob.bin"}
    assert files["committed.txt"]["status"] == "added" and not files["committed.txt"]["untracked"]
    assert files["README.md"]["additions"] == 1 and files["README.md"]["deletions"] == 0
    assert files["untracked.txt"]["untracked"] and files["untracked.txt"]["additions"] == 2
    assert files["blob.bin"]["binary"]
    assert (out["additions"], out["deletions"]) == (5, 0)
    assert sh(wt, "git", "status", "--porcelain") == before  # read-only: nothing staged or added


def worktree(env):
    return ws_mod.new(launch=False, name="show changes", repo_queries=["web"]).path / "acme-web"


def test_repo_diff_ignores_diff_settings_that_change_the_format(env):
    wt = worktree(env)
    (wt / "notes.txt").write_text("one\n\nthree\n")
    sh(wt, "git", "add", ".")
    sh(wt, "git", "commit", "-q", "-m", "notes")
    for key, value in (("diff.noprefix", "true"), ("diff.mnemonicPrefix", "true"),
                       ("diff.suppressBlankEmpty", "true")):
        sh(wt, "git", "config", key, value)
    (wt / "notes.txt").write_text("one\n\n3\n")
    (wt / "tab\tname.txt").write_text("x\n")
    sh(wt, "git", "add", "tab\tname.txt")
    files = {f["path"]: f for f in diffs.repo_diff(wt, "origin/main")["files"]}
    assert set(files) == {"notes.txt", "tab\tname.txt"}
    lines = [(ln["kind"], ln["old"], ln["new"], ln["text"]) for h in files["notes.txt"]["hunks"] for ln in h["lines"]]
    assert lines == [("+", None, 1, "one"), ("+", None, 2, ""), ("+", None, 3, "3")]  # all new since the fork


def test_untracked_links_are_not_followed(env):
    wt = worktree(env)
    secret = env.root / "secret.txt"
    secret.write_text("do not show\n")
    (wt / "link").symlink_to(secret)
    os.mkfifo(wt / "pipe")  # git doesn't list it; reading it would block
    [link] = diffs.repo_diff(wt, "origin/main")["files"]
    assert link["path"] == "link" and [ln["text"] for ln in link["hunks"][0]["lines"]] == [str(secret)]


def test_untracked_files_share_the_line_budget_and_are_capped(env, monkeypatch):
    wt = worktree(env)
    monkeypatch.setattr(diffs, "MAX_TOTAL_LINES", 5)
    monkeypatch.setattr(diffs, "MAX_UNTRACKED_FILES", 2)
    for name in ("a.txt", "b.txt", "c.txt"):
        (wt / name).write_text("1\n2\n3\n")
    out = diffs.repo_diff(wt, "origin/main")
    a, b = out["files"]
    assert (a["path"], b["path"], out["untracked_omitted"]) == ("a.txt", "b.txt", 1)
    assert len(b["hunks"][0]["lines"]) == 2 and b["truncated"] and b["additions"] == 3


def test_parse_reads_a_blank_context_line_without_its_space_and_quoted_paths():
    [f] = diffs.parse('diff --git "a/tab\\tname" "b/tab\\tname"\n--- "a/tab\\tname"\n+++ "b/tab\\tname"\n'
                      "@@ -1,3 +1,3 @@\n one\n\n-three\n+3\n")
    assert f.path == "tab\tname"
    assert [(ln.kind, ln.old, ln.new) for ln in f.hunks[0].lines] == [
        (" ", 1, 1), (" ", 2, 2), ("-", 3, None), ("+", None, 3)]
    [g] = diffs.parse('diff --git "a/caf\\303\\251" "b/caf\\303\\251"\nnew file mode 100644\n'
                      "Binary files /dev/null and \"b/caf\\303\\251\" differ\n")
    assert (g.path, g.binary) == ("café", True)
