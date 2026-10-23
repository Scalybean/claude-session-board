#!/usr/bin/env python3
"""Session board - setup: everything optional is decided here, by the person using the board, and nothing is changed
before they say yes. The logic lives in this module (no screen code); the board's first start shows csb_wizard.py,
which collects the choices and calls apply().

    python3 csb_setup.py --status       what is set up, from setup.json
    python3 csb_setup.py --uninstall    remove everything setup added (hook entries, CLAUDE.md block, skill, ct, configs)

What it can change, and only for the items that were ticked (each is recorded in <session-board>/setup.json so that it
can be undone exactly):
  ~/.claude/settings.json     hook entries and the status line (a timestamped backup is written first)
  ~/.claude/CLAUDE.md         one block between <!-- claude-tasks:start --> and <!-- claude-tasks:end -->
  ~/.claude/skills/task-intake/SKILL.md, ~/.local/bin/ct
  <session-board>/            ct.json, todoist.json, ntfy.json, cloud/setup-script.sh
  Claude Desktop's config     the chat add-on (Windows)"""
import json, os, re, secrets, shlex, shutil, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from csb_paths import find_dir, has_tabs, is_wsl, windows_homes

MARK_START, MARK_END = "<!-- claude-tasks:start -->", "<!-- claude-tasks:end -->"
VERSION = 1

# ---------- what can be chosen ----------
# (id, group, title, what it does)
ADDONS = [
    ("track", "Show your sessions on the board", "adds a state hook to Claude Code's settings.json (backup first) so every session reports its state; without it the board stays empty"),
    ("todoist", "Todoist tasks view", "a Tasks tab with your Todoist projects; needs a Todoist API token, which stays in the session-board folder"),
    ("ntfy", "Phone pushes through ntfy.sh", "when a session waits for you, sends its project name and a short line about it (or just a generic line) to a private ntfy.sh topic"),
    ("cloud", "Cloud sessions on the board", "writes a setup script for your claude.ai/code environment so cloud sessions report to the board through ntfy.sh (needs the ntfy item)"),
    ("desktop", "Claude Desktop chat add-on", "a small local MCP server so Claude Desktop chats show up on the board (Windows)"),
]
TASKS = [
    ("style", "Working-style rules", "act when enough is known, batch questions, verify before saying done, stay in scope"),
    ("tests", "Never weaken tests", "do not skip, delete or loosen tests to make them pass; say so and ask instead"),
    ("steps", "Step list for any task over two steps", "written as outcomes; this is what the board's progress bar shows"),
    ("handoff", "Multi-session handoff", "read CLAUDE.md, docs/INDEX.md and tasks/PROGRESS.md at the start; write a PROGRESS.md entry at the end"),
    ("compact", "Compaction rules", "what to keep when the conversation is compacted"),
    ("skill", "task-intake skill", "tiers (quick/routine/risky), a task packet with acceptance checks, a worktree, an independent review, a PR at the end, escalation to you"),
    ("ct", "ct launcher", "start background tasks with a router-chosen model and effort, capped at N running and N waiting for review"),
    ("g_plan", "Guard: plan nudge", "reminds Claude to keep a step list when it has none"),
    ("g_push", "Guard: push guard", "asks you before a git push to main/master"),
    ("g_tests", "Guard: test guard", "blocks the hand-back once if tests were deleted or skipped"),
    ("g_review", "Guard: review gate", "a routine/risky task packet cannot be marked done without a reviewer verdict and evidence"),
    ("usage", "Status line and session history", "plan usage in the status line; a local log of prompts and replies in <session-board>/history (never leaves this machine)"),
]
RULE_IDS = ("style", "tests", "steps", "handoff", "compact", "skill")
ALL_IDS = [i for i, *_ in ADDONS + TASKS]

def available(fid):
    """(usable here, note) - an item that cannot work on this machine is shown but cannot be ticked"""
    if fid == "desktop":
        if not has_tabs(): return False, "needs Windows (PowerShell)"
        if not desktop_config_path(): return False, "Claude Desktop's config was not found"
        if is_wsl() and not data_dir().startswith("/mnt/"): return False, "the board folder must be on the Windows side"
    if fid == "ct" and os.name == "nt": return False, "not on native Windows (ct is a shell-launched script)"
    if fid == "usage":
        cur = (read_json(settings_path()) or {}).get("statusLine")
        if cur and not is_ours(cur.get("command", "")): return True, "you already have a status line; it stays, history is still added"
    return True, ""

# ---------- places ----------
def data_dir(): return find_dir()
def claude_dir(): return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
def settings_path(): return os.path.join(claude_dir(), "settings.json")
def claude_md_path(): return os.path.join(claude_dir(), "CLAUDE.md")
def skill_path(): return os.path.join(claude_dir(), "skills", "task-intake", "SKILL.md")
def ct_path(): return os.path.join(os.path.expanduser("~"), ".local", "bin", "ct")
def state_path(): return os.path.join(data_dir(), "setup.json")

def desktop_config_path():
    if os.name == "nt": base = os.path.join(os.environ.get("APPDATA", ""), "Claude")
    elif is_wsl():
        homes = windows_homes()
        base = os.path.join(homes[0], "AppData", "Roaming", "Claude") if homes else ""
    elif sys.platform == "darwin": base = os.path.expanduser("~/Library/Application Support/Claude")
    else: return None
    p = os.path.join(base, "claude_desktop_config.json")
    return p if base and os.path.isdir(base) else None

def asset(rel):
    """a file shipped with the board: <session-board>/assets/ once installed, the repo's own copy when run from a checkout"""
    here = os.path.dirname(os.path.abspath(__file__))
    repo = {"rules.md": "../config/rules.md", "task-intake/SKILL.md": "../skills/task-intake/SKILL.md", "ct": "../bin/ct",
            "cloud/setup-template.sh": "cloud/setup-template.sh", "cloud/board-report.py": "cloud/board-report.py"}
    for p in (os.path.join(here, "assets", rel), os.path.join(data_dir(), "assets", rel), os.path.join(here, repo.get(rel, rel))):
        if os.path.isfile(p): return p
    raise FileNotFoundError(rel)

def read_text(p):
    with open(p, encoding="utf-8-sig") as f: return f.read()      # a BOM (PowerShell 5 writes one) is not part of the text

def read_raw(p):
    """the file exactly as it is on disk (line endings included), for files that are not ours alone"""
    with open(p, encoding="utf-8", newline="") as f: return f.read()

def write_text(p, text, mode=None):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f: f.write(text)
    if mode: os.chmod(tmp, mode)
    os.replace(tmp, p)

def read_json(p):
    try: return json.loads(read_text(p))
    except Exception: return None

def write_json(p, d):
    write_text(p, json.dumps(d, indent=2, ensure_ascii=False) + "\n")

# ---------- state ----------
def load_state():
    st = read_json(state_path()) or {}
    st.setdefault("version", VERSION); st.setdefault("done", False); st.setdefault("choices", {}); st.setdefault("extras", {}); st.setdefault("applied", {})
    return st

def save_state(st):
    write_json(state_path(), st)

def needs_setup():
    st = read_json(state_path()) or {}
    return not st.get("done") and not st.get("never")

# ---------- hooks ----------
def q(path):
    return '"' + path + '"' if os.name == "nt" else shlex.quote(path)

def py(script):
    """a hook command that fails open: python exits 2 when the script is missing, and exit 2 means "block" to Claude Code
    (every prompt, Bash call or Stop). Only a deliberate {"decision": "block"} on stdout may block."""
    return f"{q(sys.executable)} {q(os.path.join(data_dir(), script))} || exit 0"

def state_hook_command():
    if os.name == "nt":
        return f'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File {q(os.path.join(data_dir(), "hook.ps1"))}'
    cmd = py("csb-hook.py")
    return ('CSB_WSL_PID=$PPID WSLENV="${WSLENV:+$WSLENV:}CSB_WSL_PID" ' + cmd) if is_wsl() else cmd      # WSL: the tab-jump needs the terminal's pid

STATE_EVENTS = ["SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "PermissionRequest", "Notification", "Stop", "StopFailure", "SessionEnd"]

def hook_specs(choices):
    """the hook entries the ticked items need: dicts of event, matcher, command, async, timeout"""
    on = lambda i: choices.get(i)
    out = []
    def add(event, command, timeout, matcher=None, is_async=False):
        out.append({"event": event, "matcher": matcher, "command": command, "async": is_async, "timeout": timeout})
    if on("track"):
        for ev in STATE_EVENTS:
            add(ev, state_hook_command(), 30, "permission_prompt|elicitation_dialog|agent_needs_input" if ev == "Notification" else None, True)
    if on("track") or on("g_tests") or on("g_review"):
        # records where each session starts (the Stop guards judge only its own changes) and warns about shared folders
        add("SessionStart", py("csb-start.py"), 10); add("UserPromptSubmit", py("csb-start.py"), 10)
    if on("g_plan"):
        add("UserPromptSubmit", py("csb-plan-nudge.py"), 10); add("PostToolUse", py("csb-plan-nudge.py"), 10)
    if on("g_push"): add("PreToolUse", py("csb-push-guard.py"), 10, "Bash|PowerShell")
    if on("g_tests"): add("Stop", py("csb-test-guard.py"), 15)
    if on("g_review"): add("Stop", py("csb-review-gate.py"), 15)
    if on("usage"):
        add("UserPromptSubmit", py("csb-history.py"), 10, None, True); add("Stop", py("csb-history.py"), 10, None, True)
    return out

def is_ours(command):
    """a command that runs one of the board's scripts from this session-board folder"""
    d = data_dir().replace("\\", "/").lower()
    c = (command or "").replace("\\", "/").lower()
    forms = {d, re.sub(r"^/mnt/([a-z])/", r"\1:/", d)}        # /mnt/c/Users/x and C:/Users/x are the same folder
    return any(f in c for f in forms) and re.search(r"(hook\.ps1|csb-[a-z-]+\.py)", c) is not None

def script_id(command):
    """which board script a command runs; the Windows and the Python state hook count as the same one"""
    m = re.search(r"(hook\.ps1|csb-[a-z-]+\.py)", (command or "").replace("\\", "/"))
    return "state" if m and m.group(1) in ("hook.ps1", "csb-hook.py") else m.group(1) if m else None

def merge_hooks(settings, wanted, previous):
    """returns (new settings, added specs, removed count). Removes entries we added before that are no longer wanted and adds
    missing ones. An entry that is already there (wired by hand, same command or same board script) is left alone and is
    NOT reported as added, so it is never removed later."""
    hooks = settings.setdefault("hooks", {})
    key = lambda s: (s["event"], s.get("matcher") or "", s["command"])
    want, prev = {key(s): s for s in wanted}, {key(s): s for s in previous}
    removed = 0; added = []
    for k in prev:
        if k in want: continue
        for group in list(hooks.get(k[0], [])):
            if (group.get("matcher") or "") != k[1]: continue
            kept = [h for h in group.get("hooks", []) if h.get("command") != k[2]]
            if len(kept) != len(group.get("hooks", [])): removed += 1
            if kept: group["hooks"] = kept
            else: hooks[k[0]].remove(group)
        if not hooks.get(k[0]): hooks.pop(k[0], None)
    for k, s in want.items():
        if k in prev: continue                       # ours from an earlier run: already there
        arr = hooks.setdefault(k[0], [])
        if any(h.get("command") == k[2] for g in arr for h in g.get("hooks", [])): continue
        if any(is_ours(h.get("command")) and script_id(h.get("command")) == script_id(k[2]) for g in arr if (g.get("matcher") or "") == k[1] for h in g.get("hooks", [])):
            continue
        h = {"type": "command", "command": s["command"], "timeout": s["timeout"]}
        if s["async"]: h["async"] = True
        group = next((g for g in arr if (g.get("matcher") or "") == k[1] and len(g.get("hooks", [])) and all(is_ours(x.get("command")) for x in g["hooks"])), None)
        if group is None:
            group = {"hooks": []}
            if k[1]: group["matcher"] = k[1]
            arr.append(group)
        group["hooks"].append(h); added.append(s)
    if not settings["hooks"]: settings.pop("hooks")
    return settings, added, removed

def backup(path, note):
    if os.path.isfile(path):
        dest = f"{path}.bak-claude-tasks-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, dest); note.append(f"backup: {dest}")

# ---------- CLAUDE.md block ----------
def rules_block(choices):
    text = read_text(asset("rules.md"))
    parts = dict(re.findall(r"<!-- rule:(\w+) -->\n(.*?)(?=<!-- rule:|\Z)", text, re.S))
    body = "\n".join(parts[i].strip() + "\n" for i in RULE_IDS if choices.get(i) and i in parts)
    return f"{MARK_START}\n{body.rstrip()}\n{MARK_END}\n" if body.strip() else ""

def set_block(path, block, applied):
    """put `block` between the markers, or take it out again, leaving every other byte of the file as it was: line endings
    are kept (the block is converted to the file's), the separator we add is remembered and removed with the block, and a
    file we created is deleted when nothing else is left in it. `applied` is the record kept in setup.json."""
    exists = os.path.isfile(path); cur = read_raw(path) if exists else ""
    eol = "\r\n" if "\r\n" in cur else "\n"
    span = re.compile(re.escape(MARK_START) + r".*?" + re.escape(MARK_END) + r"(\r?\n)?", re.S)
    rec = applied.get("claude_md") if isinstance(applied.get("claude_md"), dict) else {}
    if block:
        blk = block.replace("\n", eol)
        if span.search(cur): new = span.sub(lambda m: blk, cur, count=1)
        else:
            sep = "" if not cur else eol if cur.endswith(eol) else eol * 2
            new = cur + sep + blk; rec = {"sep": sep, "created": not exists}
        applied["claude_md"] = rec or {"sep": "", "created": False}
    else:
        m = span.search(cur)
        if not m: applied["claude_md"] = False; return False
        sep = rec.get("sep", "")
        start = m.start() - len(sep) if sep and cur[m.start() - len(sep):m.start()] == sep else m.start()
        new = cur[:start] + cur[m.end():]
        applied["claude_md"] = False
        if rec.get("created") and not new.strip():
            os.remove(path); return True
    if new != cur: write_text(path, new); return True
    return False

# ---------- apply ----------
def remember(applied, name, path):
    """before setup first writes `path`, note whether it existed and keep a copy, so turning the item off puts it back"""
    files = applied.setdefault("files", {})
    if name in files: return
    keep = os.path.join(data_dir(), "setup-backup", name)
    existed = os.path.isfile(path)
    if existed: os.makedirs(os.path.dirname(keep), exist_ok=True); shutil.copy2(path, keep)
    files[name] = {"existed": existed}

def give_back(applied, name, path):
    """undo remember(): the original file comes back, or the file setup created is deleted"""
    rec = applied.setdefault("files", {}).pop(name, None)
    if rec is None: return
    keep = os.path.join(data_dir(), "setup-backup", name)
    if rec.get("existed") and os.path.isfile(keep): shutil.copy2(keep, path); os.remove(keep)
    elif os.path.isfile(path): os.remove(path)

def apply(choices, extras=None, st=None, say=print):
    """make the machine match `choices` (id -> bool), adding what is newly ticked and removing what was ticked before and
    is not any more. Returns the lines to show. Nothing outside the places in this module's docstring is touched."""
    extras = extras or {}; st = st if st is not None else load_state(); applied = st["applied"]; log = []
    choices = {i: bool(choices.get(i)) and available(i)[0] for i in ALL_IDS}
    if choices.get("cloud") and not choices.get("ntfy"): choices["cloud"] = False       # cloud sessions report through the ntfy topic
    os.makedirs(data_dir(), exist_ok=True)

    # 1. settings.json: hooks and status line
    sp = settings_path(); wanted = hook_specs(choices); prev = applied.get("hooks", [])
    want_status = bool(choices.get("usage")); had_status = applied.get("statusLine")
    if wanted or prev or want_status or had_status:
        raw = read_text(sp) if os.path.isfile(sp) else None
        settings = json.loads(raw) if raw and raw.strip() else {}
        if not isinstance(settings, dict): raise ValueError(f"{sp} is not a JSON object; leaving it alone")
        before = json.dumps(settings, sort_keys=True)
        settings, added, removed = merge_hooks(settings, wanted, prev)
        wkeys = {(w["event"], w.get("matcher") or "", w["command"]) for w in wanted}
        applied["hooks"] = [p for p in prev if (p["event"], p.get("matcher") or "", p["command"]) in wkeys] + added
        cur = settings.get("statusLine")
        if want_status and not cur: settings["statusLine"] = {"type": "command", "command": py("csb-statusline.py")}; applied["statusLine"] = settings["statusLine"]["command"]
        elif had_status and not want_status and cur and cur.get("command") == had_status: settings.pop("statusLine"); applied.pop("statusLine", None)
        elif not want_status: applied.pop("statusLine", None)
        if json.dumps(settings, sort_keys=True) != before:
            backup(sp, log); write_json(sp, settings)
            log.append(f"settings.json: {len(added)} hook entr{'y' if len(added) == 1 else 'ies'} added, {removed} removed")

    # 2. ~/.claude/CLAUDE.md block
    block = rules_block(choices)
    if block or applied.get("claude_md"):
        if block and os.path.isfile(claude_md_path()) and MARK_START not in read_raw(claude_md_path()): backup(claude_md_path(), log)
        if set_block(claude_md_path(), block, applied): log.append("CLAUDE.md: " + ("rules block written" if block else "rules block removed"))

    # 3. the task-intake skill
    if choices.get("skill"):
        src, dst = read_text(asset("task-intake/SKILL.md")), skill_path()
        if not os.path.isfile(dst) or read_text(dst) != src:
            if os.path.isfile(dst) and not applied.get("skill"): backup(dst, log)
            write_text(dst, src); log.append("skill: task-intake installed")
        applied["skill"] = True
    elif applied.get("skill"):
        if os.path.isfile(skill_path()): os.remove(skill_path()); log.append("skill: task-intake removed")
        applied["skill"] = False

    # 4. ct launcher and its limits
    if choices.get("ct"):
        remember(applied, "ct", ct_path()); remember(applied, "ct.json", os.path.join(data_dir(), "ct.json"))
        write_text(ct_path(), read_text(asset("ct")), 0o755)
        cfg = read_json(os.path.join(data_dir(), "ct.json")) or {}
        cfg["max_running"] = int(extras.get("ct_max_running") or cfg.get("max_running") or 4); cfg["max_review"] = int(extras.get("ct_max_review") or cfg.get("max_review") or 2)
        cfg.setdefault("plugins", {}); write_json(os.path.join(data_dir(), "ct.json"), cfg)
        log.append(f"ct: installed to {ct_path()} (limits {cfg['max_running']} running, {cfg['max_review']} waiting for review)")
        if not any(os.access(os.path.join(d, "ct"), os.X_OK) for d in os.environ.get("PATH", "").split(os.pathsep) if d): log.append(f"note: add {os.path.dirname(ct_path())} to your PATH to run ct")
        applied["ct"] = True
    elif applied.get("ct"):
        give_back(applied, "ct", ct_path()); give_back(applied, "ct.json", os.path.join(data_dir(), "ct.json"))
        log.append("ct: removed"); applied["ct"] = False

    # 5. Todoist
    tp = os.path.join(data_dir(), "todoist.json")
    if choices.get("todoist"):
        if extras.get("todoist_token"): remember(applied, "todoist.json", tp); write_json(tp, {"token": extras["todoist_token"].strip()}); log.append("todoist: token saved in the session-board folder")
        applied["todoist"] = True
    elif applied.get("todoist"):
        give_back(applied, "todoist.json", tp)
        log.append("todoist: off"); applied["todoist"] = False

    # 6. ntfy pushes and cloud reporting
    npath = os.path.join(data_dir(), "ntfy.json")
    if choices.get("ntfy"):
        remember(applied, "ntfy.json", npath)
        cfg = read_json(npath) or {}
        cfg.update({"enabled": True, "server": cfg.get("server", "https://ntfy.sh"), "topic": cfg.get("topic") or "claude-" + secrets.token_urlsafe(16),
                    "detail": bool(extras.get("ntfy_detail", cfg.get("detail", True)))})
        if choices.get("cloud"): cfg["board_topic"] = cfg.get("board_topic") or "claude-board-" + secrets.token_urlsafe(16)
        else: cfg.pop("board_topic", None)
        write_json(npath, cfg)
        log.append(f"ntfy: subscribe to '{cfg['topic']}' on {cfg['server']} in the ntfy app" + ("" if cfg["detail"] else " (generic text only)"))
        applied["ntfy"] = True
    elif applied.get("ntfy"):
        give_back(applied, "ntfy.json", npath)
        log.append("ntfy: pushes off"); applied["ntfy"] = False
    cloud_script = os.path.join(data_dir(), "cloud", "setup-script.sh")
    if choices.get("cloud") and choices.get("ntfy"):
        topic = read_json(npath)["board_topic"]
        tpl = read_text(asset("cloud/setup-template.sh"))
        out = (tpl.replace("{{BOARD_REPORT}}\n", read_text(asset("cloud/board-report.py")).replace("__BOARD_TOPIC__", topic))
                  .replace("{{CLOUD_CLAUDE_MD}}\n", rules_block(choices).replace(MARK_START + "\n", "").replace(MARK_END + "\n", "") or "\n")
                  .replace("{{TASK_INTAKE_SKILL}}\n", read_text(asset("task-intake/SKILL.md"))))
        write_text(cloud_script, out); log.append(f"cloud: setup script written to {cloud_script} - paste it into your claude.ai/code environment and allow ntfy.sh there")
        applied["cloud"] = True
    elif applied.get("cloud"):
        if os.path.isfile(cloud_script): os.remove(cloud_script)
        log.append("cloud: setup script removed"); applied["cloud"] = False

    # 7. Claude Desktop chat add-on
    dp = desktop_config_path()
    if choices.get("desktop") and dp:
        cfg = read_json(dp) or {}
        ps1 = os.path.join(data_dir(), "mcp-server.ps1")
        if is_wsl(): ps1 = subprocess.run(["wslpath", "-w", ps1], capture_output=True, text=True).stdout.strip() or ps1
        entry = {"command": "powershell.exe", "args": ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", ps1]}
        if cfg.get("mcpServers", {}).get("session-board") != entry:
            backup(dp, log); cfg.setdefault("mcpServers", {})["session-board"] = entry; write_json(dp, cfg)
            log.append("desktop: chat add-on added - restart Claude Desktop")
        applied["desktop"] = True
    elif applied.get("desktop") and dp:
        cfg = read_json(dp) or {}
        if cfg.get("mcpServers", {}).pop("session-board", None) is not None: backup(dp, log); write_json(dp, cfg); log.append("desktop: chat add-on removed")
        applied["desktop"] = False

    st.update(done=True, choices=choices, extras={k: v for k, v in extras.items() if k != "todoist_token"})
    save_state(st)
    return log or ["nothing needed changing"]

def uninstall():
    st = load_state()
    try: return apply({}, {}, st)
    except (ValueError, OSError) as e: return ["Setup could not finish: " + str(e)]

def status_lines():
    st = load_state()
    if not st.get("done"): return ["setup has not been run yet"]
    on = [t for i, t, _ in ADDONS + TASKS if st["choices"].get(i)]
    return ["on: " + (", ".join(on) or "nothing")]

if __name__ == "__main__":
    if "--uninstall" in sys.argv: print("\n".join(uninstall()))
    else: print("\n".join(status_lines()))
