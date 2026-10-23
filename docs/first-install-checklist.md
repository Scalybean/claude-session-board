# First install on a new platform: checklist

For a friend trying the board on **macOS** or **native Windows** (no WSL), the two platforms nobody has run yet. It takes about 20 minutes. Send back the answers marked **report**, plus any error text, copied exactly.

Before you start: you have Claude Code installed and have used it at least once, and you have Python 3.9+ and git. Note your OS version, `python3 --version` (Windows: `py --version`) and `claude --version` (**report**).

## 1. Install
1. Clone the repo and run `./install.sh` (Windows: `py install.py`). **Report:** did it finish, and where did it say it put things?
2. Open a new terminal and run `csb`. You should get the first-run setup. **Report:** does every screen fit and read correctly? Do arrows and Enter work?
3. Tick everything except Todoist and the Desktop add-on (unless you use them), then finish.
4. Back up your settings first, then open `~/.claude/settings.json` and check that the new hook commands end in `|| exit 0`. **Report:** paste the `hooks` section, with any secrets removed.

## 2. One session
1. In another terminal, `cd` into any git repo and start `claude`. Ask it something that takes a few steps.
2. **Report:** does the session show up on the board within a few seconds, and does its state change (working → your turn)?
3. On macOS: when it finishes after a run longer than 2 minutes, did a notification appear? **Report.**
4. Press `a` on the session. **Report:** what happened (on macOS it should copy a command instead of switching tabs)?

## 3. Two sessions in one folder
1. Start a second `claude` in the **same** repo folder and send it a prompt.
2. **Report:** does the board show "⚠ same folder as …" on both rows? Did the second session mention another session working in that folder?

## 4. Guards
In a scratch repo, not one you care about:
1. Ask Claude to delete a test file and then stop. **Report:** did it get blocked once with "Test guard: …"?
2. Ask Claude to run `echo "git push later"`. **Report:** it should **not** ask for permission.
3. Rename your session-board folder for a moment and send a prompt in a running session. **Report:** the session must keep working normally, with no block. Then rename the folder back.

## 5. Uninstall
1. Run `csb --uninstall`. **Report:** is `settings.json` back to how it was before, apart from formatting?

Anything that surprised you, even if it "worked": **report** it too.
