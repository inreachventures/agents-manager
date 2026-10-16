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
