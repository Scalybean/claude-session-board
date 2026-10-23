# Board: start a session in a folder, and jump to a session with a
status: done
goal: From the terminal board, `s` starts a new interactive Claude session in a folder picked with a folder browser (new Windows Terminal tab, switched to). `a` on a session that is not in the background jumps to it: its Windows Terminal tab, or the Claude desktop app for Desktop chats.
tier: risky
acceptance:
1. `s` opens a folder picker (recent session folders, then a browsable directory list; a path can be typed); choosing a folder opens a new Windows Terminal tab with `claude` running in that folder, the tab becomes active, and the session appears on the board.
   check: real run in Windows Terminal from WSL; unit check of the picker's listing/keys with a fake folder tree
2. `a` on a live terminal session makes its WT tab active (window restored/raised), even when two tabs have similar titles.
   check: real run against an open session tab; the marker title makes the match exact
3. `a` on a Desktop chat brings the Claude desktop window to front and copies the chat title; no keystrokes are sent.
   check: real run with Claude desktop open
4. `a` on background sessions is unchanged (attach); on exited sessions a clear message; on cloud sessions it copies the link.
   check: code path review + unit check of the dispatch
5. If the tab can't be found, a message says so and nothing else happens (the terminal's title is put back).
   check: run against a session whose tab doesn't exist / a non-WT terminal
definition-of-done:
- [x] all acceptance checks run, evidence below
- [x] `python3 -m py_compile` on changed files; TUI starts and renders
- [x] independent review of the diff
- [x] README / docstring key list updated
- [x] merged into main and installed with ./install.sh (user approved: "don't stop, merge and install and everything")
context: board/csb-tui.py (App.key, frame, _compose, attach), board/hook.ps1 (term = wsl:<distro>:<claude pid> | win:<pid>), board/mcp-server.ps1 (chat ids are the board's own, not claude.ai ids)
constraints: must stay stdlib-only, Python 3.8+, Windows PowerShell 5.1 compatible; must not send keystrokes to other apps; must not change attach/reply for background sessions
review-triggers: none - user approved going straight through to merge and install
decisions:
- Key `s` for the new session (`n` is already "new task via ct").
- New session = `wt.exe -w 0 new-tab wsl.exe -d <distro> --cd <folder> -- bash -lic "claude || true && exec bash"`: no `;` (wt treats it as a command separator); the shell stays open in the folder after claude exits. No `--title`, so Claude Code can still set the tab title.
- Tab lookup: Windows Terminal tabs are readable and selectable with UI Automation (TabItem, Name = the tab title Claude Code sets). To make the match exact, the board writes a unique OSC title to the session's own tty (/proc/<pid>/fd -> /dev/pts/N) while one PowerShell process looks for that tab, then writes the original title back. Windows-native sessions (`win:` terms, no tty) fall back to matching the tab title against the session's title.
- Desktop chats: the app's window is brought to front only; Electron exposes no chat list to UI Automation and the board's chat ids are not claude.ai ids, so a specific chat can't be opened.
- Desktop chat focus uses AttachThreadInput instead of the Alt-key trick: an Alt press could open the Electron app's menu bar, and no keystrokes were wanted.
- Out of scope, noted: read_key reads at most 8 bytes after ESC, so several arrow keys arriving in one burst (e.g. key repeat over a slow link) count as one. Pre-existing.
reviewer: fixed: picker paths on Windows-native Python (local_dir now leaves Windows paths alone), Claude app closed to the tray (opens claude:// and waits for its window), Tab no longer slips past the picker, a hung PowerShell is killed | open: none
evidence: unit 13/13 + tab-routing check pass after fixes; live jump, not-found, chat focus and new-tab runs below
- unit (fake folder tree, stubbed actions): 13/13 pass - recent folders (newest first, no worktrees/chats/missing), listing (no hidden, no files, sorted), → / ← keep position, enter starts in folder with a space, / path box, non-folder message, esc closes, a dispatch (bg attach, terminal jump, chat app, exited message)
- live a, terminal: jump to the GITLAB tab -> UIA shows it selected; title back to "✳ GITLAB_CA_FILE path in finish-cicd-setup.sh". Title-match fallback switched back to the dashboard tab.
- live a, not found: fake `claude` on a pty no tab shows -> "Its terminal tab was not found in Windows Terminal" after 3 s; 62 marker writes, last title written = the session title. Dead pid -> "Its terminal is gone".
- live a, chat: "Claude app in front · chat title copied"; GetForegroundWindow == Claude window.
- live s: new tab in ~/projects/claude-tasks ran claude, board recorded session e113534b with that cwd; folder with a space: tab opened, became the selected tab, `bash -lic claude || true && exec bash` + claude running with that cwd.
- render: real board in a pty, s shows the picker (recent + ~/projects), / opens the prefilled path box, esc returns to sessions, q exits 0.
log:
- 2026-10-18 intake done, packet approved (user: go straight through, merge and install)
- 2026-10-18 built and verified s and a; test tabs opened in the user's Windows Terminal during live checks (one leftover "wsl.exe" tab to close)
