**Workstreams.app**, the native macOS dashboard for `wm`, with `wm` and a Python of its own inside: nothing else to
install apart from tmux, git and Claude Code (`gh` for PR and CI status). Needs macOS 14 or later.

Download the disk image for your Mac: `apple-silicon` for M-series chips, `intel` for Intel. Open it and drag
Workstreams to Applications.

**First launch.** The app isn't signed with an Apple Developer ID, so macOS blocks it the first time as from an
unidentified developer:

- macOS 15 and later: open it once and close the warning, then in System Settings → Privacy & Security, scroll down
  and click **Open Anyway** next to "Workstreams" (it asks for your password once).
- macOS 14: Control-click Workstreams in Applications and choose **Open**.
- Or, in Terminal: `/usr/bin/xattr -dr com.apple.quarantine /Applications/Workstreams.app`
  (use the full path: a Python `xattr` installed with pip can shadow macOS's own on your PATH, and it doesn't know
  `-r`)

After that it opens normally. After an update macOS may ask again for the microphone (Claude Code's voice
dictation), because each build is signed separately.

To build it yourself instead, or to use `wm` from the terminal, see the
[README](https://github.com/inreachventures/agents-manager#readme).
