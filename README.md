# claude-tasks

A terminal board for everything Claude is doing, and a set of instructions that make delegated tasks go well.

The **session board** (`csb`) lists every Claude Code session you run - terminal, background, Claude Desktop chats, cloud - with what each one is doing, which ones are waiting for you, how far its step list has got, tokens, and plan usage. From the board you can attach to a background session, reply, end it, resume it, start a new one in a folder you pick, and search them all.

The **task-management part** is optional and chosen item by item: rules for Claude (step lists, "verify before done", never weaken tests), a `task-intake` skill (sizing a task, a written packet with acceptance checks, an independent review, a PR at the end), a `ct` launcher for background tasks, and guard hooks that enforce parts of it.

## Requirements

- Python 3.8 or newer
- Claude Code (developed against 2.1.x; the board reads a few of its files that are not a public interface, so a big version jump can break parts of it)
- `git` for the task workflow; `gh` if you want PR status on the board

## Install

```
git clone <this repo> && cd claude-tasks
./install.sh            # macOS, Linux, WSL      (Windows: python install.py)
csb                     # start the board
```

The installer only **copies files** into the session-board folder (`$CSB_DIR`, or `~/.claude/session-board`; under WSL, the Windows-side folder if a board already runs there) and adds a `csb` launcher. It does not edit your Claude settings and creates no accounts, topics or tokens. Run it again to update.

## First start: you decide what is on

The first time `csb` starts it asks, with nothing ticked by default. Nothing is changed until you confirm a summary.

| Item | What it does | What it touches | What leaves your machine |
|---|---|---|---|
| Show your sessions | State hook so sessions report to the board | adds hook entries to `~/.claude/settings.json` (backup first) | nothing |
| Todoist tasks view | A Tasks tab with your projects | token in the session-board folder | calls to Todoist's API |
| Phone pushes (ntfy) | Push when a session waits for you | `ntfy.json` with a random topic | a short line per push to ntfy.sh (details optional) |
| Cloud sessions | Cloud sessions report to the board | writes a setup script you paste into claude.ai/code | cloud sessions post to your ntfy topic |
| Claude Desktop add-on | Desktop chats show on the board (Windows) | `claude_desktop_config.json` (backup first) | nothing |
| Rules and skill | Working-style, tests, step list, handoff, compaction, `task-intake` | one marked block in `~/.claude/CLAUDE.md`; the skill folder | nothing |
| `ct` launcher | Background tasks with a router-chosen model, capped at N running / N waiting for review | `~/.local/bin/ct`, `ct.json` | one short Haiku call per task to pick the model |
| Guards | plan nudge, push guard, test guard, review gate (the test guard and review gate judge only what the current session changed) | hook entries; a per-session start record in `baseline/` | nothing |
| Status line and history | plan usage in the status line; a local log of prompts and replies | status line (only if you have none), `history/` | nothing |

Every hook command ends in `|| exit 0`, so a missing or broken board script can never block a prompt, a command or a Stop. Sessions that share one git working tree get a warning, on the board and in the session itself. Once a day the board clears per-session state older than 14 days, keeps the 3 newest old install backups and drops history older than 6 months (change it in `ct.json`: `{"prune": {"days": 14, "backups": 3, "history_months": 6}}`).

`csb --setup` reopens the questions any time; unticking something removes exactly what it added. `csb --uninstall` removes everything setup added (your own settings, hooks and CLAUDE.md text are left as they were). To remove the board itself, delete the session-board folder and the `csb` launcher.

## Platforms

| | Windows (native) | Windows + WSL | macOS | Linux |
|---|---|---|---|---|
| Board, search, attach, reply, end | yes | yes | yes | yes |
| Session tracking | PowerShell hook | Python hook | Python hook | Python hook |
| Jump to a session's terminal tab, new session in a tab | Windows Terminal | Windows Terminal | copies the command instead | copies the command instead |
| Desktop notifications | toast | toast | Notification Center | `notify-send` |
| Claude Desktop chat add-on | yes | yes | not yet | n/a |
| Tested | not on a real machine | yes | not on a real machine | yes |

"Not on a real machine" means the code paths are covered by tests that force the platform, but nobody has run them there yet. Reports welcome.

## Keys

`↑↓` select · `enter` details · `Tab` tasks · `/` search · `e` show exited · `a` attach (or go to its tab) · `r` reply · `x x` end · `c` resume · `b` continue in background · `s` new session · `n` new task (`ct`) · `f` copy folder · `h` hide · `?` every key · `q` quit

## Tests

```
python3 -m unittest discover -s tests -p "test_*.py"     # or tests/smoke.sh
```

They run in a throwaway HOME and session-board folder and never touch your real `~/.claude`. They include a pty-driven run of the first-start questions and a parity check that the Python state hook writes the same record as `hook.ps1` (the parity check runs only where PowerShell is reachable).

## Layout

- `board/` - the board (`csb-tui.py`), setup (`csb_setup.py`, `csb_wizard.py`), the hooks (`csb-*.py`, `hook.ps1`), cloud reporter
- `bin/ct` - the task launcher
- `skills/task-intake/` - the skill
- `config/rules.md` - the rules setup can add to `~/.claude/CLAUDE.md`
- `install.py`, `tests/`
