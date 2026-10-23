"""Session board - the first-run setup screens. Shown the first time the board starts (and by `csb-tui.py --setup`).
Every item is unticked until you tick it; nothing is changed until you confirm the summary. The changes themselves are
made by csb_setup.apply(); this module only asks."""
import shutil, sys, textwrap

import csb_setup as su

BOLD, DIM, BLUE, GREEN, RESET = "\x1b[1m", "\x1b[2m", "\x1b[38;5;75m", "\x1b[38;5;78m", "\x1b[0m"

WELCOME = """Welcome to the Claude session board.

It shows every Claude Code session you run (terminal, background, Desktop, cloud) in one place: what each is doing, \
what is waiting for you, and how far its step list has got.

Everything beyond that is optional and decided now, in two short lists. Nothing is on until you tick it, nothing is \
changed until you confirm a summary at the end, and `csb-tui.py --setup` brings this back whenever you want to change \
your mind. Files that setup edits are backed up first, and `csb-tui.py --uninstall` removes exactly what it added."""

HOW_TASKS = """How tasks are created and managed

Claude Code does what its instructions say, so the way tasks are handled here is a set of instructions plus a few \
guards that enforce them:

  - A step list. For anything over two steps Claude keeps a list of outcomes; the board's progress bar is that list.
  - The task-intake skill sizes each task as quick, routine or risky. Routine and risky ones get a written task \
packet (goal, acceptance checks, what must be reviewed), work in their own git worktree, are reviewed by a second \
agent that sees only the diff, and end in a pull request instead of a push to main.
  - tasks/PROGRESS.md in the repo carries history between sessions: what was done, why, what is next, what is unverified.
  - ct starts such a task in the background with a model and effort chosen for it, and refuses to start more when too \
many are running or waiting for your review.
  - Guards are hooks: they remind Claude to keep a step list, ask before a push to main, block a hand-back when tests \
were deleted or skipped, and stop a task being marked done without a reviewer verdict and evidence.

Pick the parts you want. Rules go into your ~/.claude/CLAUDE.md as one marked block, added to what is already there."""

def width():
    return max(40, min(shutil.get_terminal_size((100, 30)).columns - 2, 110))

def wrap(text, indent="", w=None):
    out = []
    for para in text.split("\n"):
        out += textwrap.wrap(para, (w or width()) - len(indent), subsequent_indent="    " if para.startswith("  - ") else "") or [""]
    return [indent + l for l in out]

def show(lines):
    sys.stdout.write("\x1b[2J\x1b[H" + "\n".join(lines) + "\n"); sys.stdout.flush()

def wait_key(read_key):
    while True:
        k = read_key(1.0)
        if k is not None: return k

def page(read_key, text, footer):
    """a page of text; returns the key pressed among those the footer offers"""
    show(wrap(text) + ["", DIM + footer + RESET])
    return wait_key(read_key)

def checklist(read_key, title, intro, items, state):
    """items: (id, title, why); state: id -> bool, edited in place. Returns 'next', 'skip' or 'back'."""
    sel = 0
    while True:
        lines = [BOLD + title + RESET] + wrap(intro) + [""]
        for n, (fid, t, why) in enumerate(items):
            ok, note = su.available(fid)
            box = "[x]" if state.get(fid) else "[ ]"
            line = f" {'▸' if n == sel else ' '} {box} {t}" + (f"  {DIM}({note}){RESET}" if note else "")
            style = (BLUE if n == sel else "") + (DIM if not ok else "")
            lines.append(style + line + (RESET if style else ""))
        fid, t, why = items[sel]
        lines += ["", BOLD + t + RESET] + wrap(why, "  ") + ["", DIM + "↑↓ move · space tick · a all · n none · enter continue · b back · esc skip for now" + RESET]
        show(lines)
        k = wait_key(read_key)
        if k in ("down", "j"): sel = (sel + 1) % len(items)
        elif k in ("up", "k"): sel = (sel - 1) % len(items)
        elif k == " ":
            if su.available(fid)[0]: state[fid] = not state.get(fid)
        elif k == "a":
            for i, *_ in items:
                if su.available(i)[0]: state[i] = True
        elif k == "n":
            for i, *_ in items: state[i] = False
        elif k == "enter": return "next"
        elif k == "b": return "back"
        elif k in ("esc", "q", "\x03"): return "skip"

def ask_line(read_key, prompt, secret=False, default=""):
    """a line of text. Enter on an empty line gives `default`. Returns None on Esc."""
    buf = ""
    while True:
        sys.stdout.write("\r\x1b[K" + prompt + ("*" * len(buf) if secret else buf)); sys.stdout.flush()
        k = wait_key(read_key)
        if k == "enter": sys.stdout.write("\n"); return buf.strip() or default
        if k == "esc": sys.stdout.write("\n"); return None
        if k in ("\x7f", "\x08"): buf = buf[:-1]
        elif k.startswith("\x00paste:"): buf += k[7:].strip()
        elif len(k) == 1 and k >= " ": buf += k

def ask_yes_no(read_key, prompt, default=True):
    sys.stdout.write(prompt + (" [Y/n] " if default else " [y/N] ")); sys.stdout.flush()
    while True:
        k = wait_key(read_key)
        if k == "enter": sys.stdout.write("\n"); return default
        if k in ("y", "Y"): sys.stdout.write("yes\n"); return True
        if k in ("n", "N"): sys.stdout.write("no\n"); return False

def describe(choices, extras):
    """what applying these choices would change, one line per place"""
    on = lambda i: choices.get(i) and su.available(i)[0]
    hooks = len(su.hook_specs(choices)); lines = []
    if hooks or on("usage"): lines.append(f"{su.settings_path()}: {hooks} hook entr{'y' if hooks == 1 else 'ies'}" + (" and the status line" if on("usage") else "") + " (backup first)")
    if su.rules_block(choices): lines.append(f"{su.claude_md_path()}: one marked block with your ticked rules (the rest of the file is untouched)")
    if on("skill"): lines.append(f"{su.skill_path()}: the task-intake skill")
    if on("ct"): lines.append(f"{su.ct_path()}: the ct launcher, limits {extras.get('ct_max_running', 4)} running / {extras.get('ct_max_review', 2)} waiting for review")
    if on("todoist"): lines.append(f"{su.data_dir()}: Todoist" + (" token" if extras.get("todoist_token") else " (paste the token on the Tasks tab)"))
    if on("ntfy"): lines.append(f"{su.data_dir()}/ntfy.json: a private ntfy.sh topic" + ("" if extras.get("ntfy_detail", True) else ", generic text only") + " - the only thing that leaves this machine")
    if on("cloud"): lines.append(f"{su.data_dir()}/cloud/setup-script.sh: for your claude.ai/code environment (you paste it there)")
    if on("desktop"): lines.append(f"{su.desktop_config_path()}: the chat add-on (backup first)")
    return lines or ["nothing - everything stays off"]

def run(read_key, force=False):
    """returns 'applied', 'skipped' or 'never'. Caller has the terminal in cbreak mode."""
    st = su.load_state(); state = dict(st["choices"]); extras = dict(st["extras"])
    if not force:
        k = page(read_key, WELCOME, "enter: set up · s: skip for now (asked again next time) · n: don't ask again")
        if k == "s" or k == "esc": return "skipped"
        if k == "n":
            st["never"] = True; su.save_state(st); return "never"
    stage = 0
    while True:
        if stage == 0:
            r = checklist(read_key, "Add-ons", "What the board can connect to. Each item says what it does and what it touches.", [(i, t, w) for i, t, w in su.ADDONS], state)
            if r == "skip": return "skipped"
            if r == "back": continue
            if state.get("todoist") and not extras.get("todoist_token"):
                show(wrap("Todoist: paste your API token (Todoist → Settings → Integrations → Developer). It is saved in the session-board folder only. Enter with nothing pasted skips it; you can paste it later on the Tasks tab."))
                tok = ask_line(read_key, "token: ", secret=True)
                if tok: extras["todoist_token"] = tok
            if state.get("cloud") and not state.get("ntfy"):
                state["ntfy"] = True
                show(wrap("Cloud sessions report to the board through an ntfy.sh topic, so phone pushes are switched on too. You can untick it again on the previous screen (that turns cloud off as well)."))
                wait_key(read_key)
            if state.get("ntfy"):
                show(wrap("Phone pushes: ntfy.sh topics are public addresses that are only hard to guess. Should a push say what the session did or asks (the reply, the command), or only the project name and a generic line?"))
                extras["ntfy_detail"] = ask_yes_no(read_key, "Include the details?", default=False)
            stage = 1
        elif stage == 1:
            k = page(read_key, HOW_TASKS, "enter: choose · b: back · esc: skip for now")
            if k == "b": stage = 0
            elif k == "esc": return "skipped"
            else: stage = 2
        elif stage == 2:
            r = checklist(read_key, "Task management", "Tick what you want Claude to follow. Every box is off until you tick it.", [(i, t, w) for i, t, w in su.TASKS], state)
            if r == "skip": return "skipped"
            if r == "back": stage = 1; continue
            if state.get("ct"):
                show(wrap("ct limits: it refuses to start more tasks when too many are running or waiting for your review. Enter keeps the default."))
                for key, label, d in (("ct_max_running", "most tasks running at once", 4), ("ct_max_review", "most finished tasks waiting for your review", 2)):
                    v = ask_line(read_key, f"{label} [{extras.get(key, d)}]: ", default=str(extras.get(key, d)))
                    extras[key] = int(v) if v and v.isdigit() and int(v) > 0 else d
            stage = 3
        else:
            show([BOLD + "Summary" + RESET, ""] + wrap("\n".join("  - " + l for l in describe(state, extras))) + ["", DIM + "enter: apply · b: back · esc: skip for now" + RESET])
            k = wait_key(read_key)
            if k == "b": stage = 2; continue
            if k == "esc": return "skipped"
            if k != "enter": continue
            try: log = su.apply(state, extras)
            except Exception as e: log = ["Setup stopped: " + str(e), "Nothing was changed in the file it stopped on."]
            show([GREEN + BOLD + "Done" + RESET, ""] + wrap("\n".join("  - " + l for l in log)) + ["", DIM + "press any key" + RESET])
            wait_key(read_key)
            return "applied"
