# Workstreams.app polish: trust, auto-update, opaque sidebar, softer terminal

WS-7. Four changes to Workstreams.app (`macos/`) and `wm` (`src/agentmgr/`), as requested:

1. Start Claude without the folder-trust dialog appearing for every new workstream.
2. Update the app automatically from the latest GitHub release.
3. Make the left panel opaque: it is translucent, and over a white window it looks bevelled.
4. Give the terminal pane a palette closer to Ghostty's. The near-black background with white text is harsh.

They ship as four independent PRs against `main`, one per item:

| Item | Branch |
|---|---|
| Folder trust | `ws-7-trust` |
| Opaque sidebar | `ws-7-sidebar` |
| Terminal palette | `ws-7-terminal-palette` |
| Sparkle auto-update, plus this spec | `ws-7-app-polish-updates` |

## Goals and non-goals

| | |
|---|---|
| Goals | No trust dialog in new workstreams. In-app updates with the standard "new version available" prompt and a "Check for Updates…" menu item. Opaque sidebar. Ghostty's default palette in dark mode. |
| Non-goals | Developer ID signing and notarisation. Apple Developer Program enrolment is under way; switching the build over is a follow-up. Until then macOS still asks again for the microphone after each update. |
| Anti-goals | An update that installs without asking. Any edit to `~/.claude.json` beyond the one trust flag, or one that could corrupt the file. A terminal that changes in light mode. |

## 1. Folder trust

**Finding.** Claude Code accepts trust from a parent folder. Tested on Claude Code 2.1.325: `claude` started in a new subfolder of an already-trusted `~/workstreams/WS-7` showed no dialog. Today each `~/workstreams/WS-N` has its own `hasTrustDialogAccepted` entry in `~/.claude.json` (the dialog was answered every time), and `~/workstreams` itself has none.

**Approaches**

| | Trade-off |
|---|---|
| **A. `wm` trusts the workstreams folder once (picked)** | One entry covers every workstream. It writes to Claude Code's own state file, which running sessions also rewrite. |
| B. Trust each new workstream folder as it's created | The same write, with one entry per workstream for no gain. |
| C. Leave it to the user (`wm doctor` already says how) | No code, but this is the status quo the change removes. |

**Design**
- A new function in `wm` makes sure `cfg.workstreams_dir` is trusted. It reuses `cli._trusted`'s rule (the folder or any ancestor has `hasTrustDialogAccepted: true`), moved somewhere shared.
- If the folder isn't trusted, it sets `projects["<workstreams_dir>"].hasTrustDialogAccepted = true`. It keeps any other keys already in that entry and leaves every other key in the file untouched.
- It writes the file atomically: a temp file in the same folder, then `os.replace`, keeping the file mode.
- If `~/.claude.json` is missing or not valid JSON, it does nothing: no new file, no error. Claude Code then shows the dialog as before.
- It runs when a workstream is created, before its session starts. That self-heals if a running Claude session overwrites the file with its older in-memory copy and drops the entry.
- `wm doctor` keeps its check and adds that the next new workstream fixes it.
- Tests use a temporary `HOME`. Cover: an untrusted folder gets trusted; an already-trusted folder, or one with a trusted ancestor, is left alone (the file isn't touched); other keys and entries survive; a missing file and invalid JSON are both no-ops.

## 2. Auto-update with Sparkle 2

**Approaches**

| | Trade-off |
|---|---|
| **Sparkle 2 (picked)** | The standard for Mac apps outside the App Store. It handles download, signature check, replacing the app, relaunching and failure cases, and it doesn't need a Developer ID. It adds a framework and a signing key to look after. |
| Homemade updater (GitHub API, download the DMG, swap, relaunch) | No dependency, but it would rebuild what Sparkle already does well: replacing a running app, quarantine, half-finished installs. |

**Design**
- **Dependency.** Sparkle 2 as a SwiftPM dependency in `macos/Package.swift`, pinned to an exact version.
- **Bundling.** `build-app.sh` copies `Sparkle.framework` into `Contents/Frameworks/`, keeping its symlinks, and gives the binary an rpath that finds it there.
- **Signing.** `sign()` signs the framework ad hoc from the inside out (its helper apps and XPC services, then the framework) before signing the app.
- **Feeds, one per build.** Each build carries its own `SUFeedURL`:
  - `https://github.com/inreachventures/agents-manager/releases/latest/download/appcast-apple-silicon.xml`
  - `https://github.com/inreachventures/agents-manager/releases/latest/download/appcast-intel.xml`

  The repo is public, and `latest/download` redirects to the newest release's asset, so there's nothing extra to host. A universal build is ruled out because the bundled Python would double the download.
- **Info.plist.** `SUPublicEDKey` (the public half of the key below) and `SUEnableAutomaticChecks = true`, using Sparkle's default daily check.
  - `CFBundleVersion` becomes the release version. It is fixed at `1` today, and Sparkle compares it against each version listed in the feed.
  - `SUAutomaticallyUpdate` is not set, so Sparkle asks before installing.
  - The development build (`build-app.sh` with no `--release`) has no feed URL, so it never offers to replace itself.
- **App.** An `SPUStandardUpdaterController` is created at launch, and "Check for Updates…" goes in the app menu after "About".
- **CI, on a `v*` tag only.**
  - Sign each DMG with Sparkle's `sign_update` and the secret `SPARKLE_ED_PRIVATE_KEY`.
  - Before publishing, check each DMG as the app will (`macos/scripts/check-update.swift`): the app inside reads that chip's feed, has the tag's version, and its `SUPublicEDKey` accepts the signature. A secret that doesn't match the app's key fails the release instead of shipping an update every copy would refuse.
  - Write `appcast-<chip>.xml` for each chip. Each feed has one item: the version, the signature and length, `sparkle:minimumSystemVersion` 14.0, and a link to the GitHub release page for the notes.
  - Attach both feeds to the release with the DMGs.
  - `sign_update` is a macOS tool, so this runs on a macOS runner: the release job moves to `macos-15`, or the signing happens in the build job when the run is for a tag.
- **Signing key.**
  - Generated once with Sparkle's `generate_keys`. The maintainers keep the private key; CI gets it as the GitHub secret `SPARKLE_ED_PRIVATE_KEY`.
- **Docs.** `.github/release-notes.md` and `macos/README.md` say updates are now in-app from 0.5.0, and that 0.5.0 itself has to be installed by hand.

**Verification before release**
- Build two local release builds, an "old" one and a "new" one.
- Serve the new DMG and its feed from a local web server, and point the old build at it through a test-only feed override (a user default; the builder picks the mechanism).
- Pass when the old build offers the update, installs it and relaunches as the new version, with the ad-hoc signature intact.
- The first real tag (`v0.5.0`) then checks the CI half: the feeds are attached, and the `latest/download` URLs resolve.

## 3. Opaque sidebar

- `SidebarView` uses `.listStyle(.sidebar)` inside a `NavigationSplitView`, which gives it macOS's translucent sidebar material.
- Hide the list's scroll background and put `Color(nsColor: .windowBackgroundColor)` behind it.
- Keep the sidebar list style, since its row selection and layout are wanted.
- Check: a screenshot with a white window behind the app, in light and dark mode, shows no see-through.

## 4. Terminal palette

Ghostty on this Mac runs its built-in default theme (`ghostty +show-config --default`):

| Role | Value |
|---|---|
| Background | `#282c34` |
| Foreground | `#ffffff` |
| ANSI 0–7 | `#1d1f21 #cc6666 #b5bd68 #f0c674 #81a2be #b294bb #8abeb7 #c5c8c6` |
| ANSI 8–15 | `#666666 #d54e53 #b9ca4a #e7c547 #7aa6da #c397d8 #70c0b1 #eaeaea` |

- In dark mode, `WMTerminalView` uses this background and foreground and installs these 16 colours (SwiftTerm `installColors`).
- `WMTerminalView.background` also paints the area around the terminal in `ContentView`, so that changes with it.
- Light mode keeps today's colours: white background, near-black text, SwiftTerm's default ANSI colours.
- The cursor (accent colour) and the selection colour stay as they are.

**Revised after review: neutral background.** On screen, Ghostty's blue-grey `#282c34` clashed with the app's
neutral greys (sidebar, toolbar, Changes panel). two fixes were compared in screenshots:

| Option | Change | Outcome |
|---|---|---|
| A. One blue-grey family | Sidebar and toolbar `#21252b`, terminal and Changes panel `#282c34`, dividers `#181a1f` | Not picked |
| **B. Neutral terminal (picked)** | Terminal background `#27272a`; Ghostty's foreground and 16 ANSI colours kept; app chrome unchanged | Shipped in #13 |

## Left to the builder
- The exact Sparkle version, and how the binary finds the framework (rpath flag in `swift build` or `install_name_tool`).
- The test-only feed override mechanism.
- Whether CI signing lives in the release job (moved to macOS) or in the build job on tags.
- Where the shared trust helper lives (`config.py` or a new small module).
