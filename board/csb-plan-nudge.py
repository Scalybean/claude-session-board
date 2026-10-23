#!/usr/bin/env python3
"""Session board - plan nudge. A synchronous UserPromptSubmit + PostToolUse hook that makes sure every session keeps
a real step list, so the board can show progress instead of raw commands. Silent once the session has a plan.

A plan is: a TaskCreate/TodoWrite call in the transcript, or a checklist file plans/<session_id>.md here
('- [ ] todo', '- [>] doing', '- [x] done'), for sessions that have no task tool."""
import glob, json, os, re, sys

NUDGE_AT = (4, 15, 40)      # tool calls without a plan before reminding again

from csb_paths import find_dir

def to_local(p):
    """a Windows path handed to us by a Windows session, as seen from WSL"""
    m = re.match(r"^([A-Za-z]):[\\/](.*)", p or "")
    if m and os.name != "nt": return "/mnt/" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/")
    return p

def to_windows(p):
    m = re.match(r"^/mnt/([a-z])/(.*)", p)
    return (m.group(1).upper() + ":\\" + m.group(2).replace("/", "\\")) if m else p

def main():
    ev = json.load(sys.stdin)
    if ev.get("agent_id") or not ev.get("session_id"): return     # subagents are short; the parent's plan covers them
    sid, name = ev["session_id"], ev.get("hook_event_name")
    if not os.path.isdir(find_dir()): return      # no board here: nothing would show the plan, and don't create its folder
    plans = os.path.join(find_dir(), "plans"); os.makedirs(os.path.join(plans, ".state"), exist_ok=True)
    with lock(os.path.join(plans, ".state", sid + ".lock")): run(ev, sid, name, plans)

def lock(path):
    """parallel tool calls run this hook at the same time: one at a time per session (flock; no lock where it's missing)"""
    import contextlib, time
    @contextlib.contextmanager
    def held():
        f = None
        try:
            import fcntl
            f = open(path, "w"); end = time.time() + 3
            while True:
                try: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB); break
                except OSError:
                    if time.time() > end: break
                    time.sleep(0.03)
        except Exception:
            pass
        try: yield
        finally:
            if f: f.close()
    return held()

def run(ev, sid, name, plans):
    plan_file = os.path.join(plans, sid + ".md")
    state_file = os.path.join(plans, ".state", sid + ".json")
    try:
        with open(state_file, encoding="utf-8") as fh: st = json.load(fh)
    except Exception:
        st = {"off": 0, "tools": 0, "plan": False, "nudged": []}
    st.setdefault("prompts", 0); st.setdefault("nudged_prompt", -1); st.setdefault("tools_at_nudge", -1)
    if name == "UserPromptSubmit": st["prompts"] += 1
    tp = to_local(ev.get("transcript_path") or "")
    if not st["plan"] and tp and os.path.exists(tp):
        size = os.path.getsize(tp)
        if size < st["off"]: st["off"] = 0
        with open(tp, "rb") as fh:
            fh.seek(st["off"]); data = fh.read()
        st["off"] += len(data)
        st["tools"] += len(re.findall(rb'"type":\s*"tool_use"', data))
        if re.search(rb'"name":\s*"(TaskCreate|TodoWrite)"', data): st["plan"] = True
    if name == "UserPromptSubmit" and os.path.exists(plan_file):
        # a new request after a finished checklist starts a new plan: archive the old one so progress restarts at 0
        try:
            items = re.findall(r"^\s*[-*]\s*\[([ xX>~])\]", open(plan_file, encoding="utf-8").read(), re.M)
            if items and all(i in "xX" for i in items):
                os.makedirs(os.path.join(plans, "archive"), exist_ok=True)
                os.replace(plan_file, os.path.join(plans, "archive", sid + "-" + str(int(os.path.getmtime(plan_file))) + ".md"))
                st["plan"] = False; st["nudged"] = []
        except Exception:
            pass
    if os.path.exists(plan_file): st["plan"] = True
    msg = None
    if not st["plan"]:
        # at most once per prompt; a new prompt only reminds again if tools ran since the last reminder ("thanks!" stays quiet)
        if name == "UserPromptSubmit": due = st["tools"] != st["tools_at_nudge"]
        else: due = st["nudged_prompt"] != st["prompts"] and any(st["tools"] >= n and n not in st["nudged"] for n in NUDGE_AT)
        if due:
            st["nudged"] = [n for n in NUDGE_AT if st["tools"] >= n]
            st["nudged_prompt"], st["tools_at_nudge"] = st["prompts"], st["tools"]
            shown = to_windows(plan_file) if re.match(r"^[A-Za-z]:", ev.get("cwd") or "") else plan_file
            msg = ("Session board: this session has no step list yet. If the work takes more than two steps, write one now so the "
                   "user can see real progress. Use TaskCreate (or TodoWrite) if you have it; otherwise create the file "
                   f"{shown} with one '- [ ] step' line per step, and mark steps '- [>]' when you start them and '- [x]' when done. "
                   "Write steps as outcomes in plain words (e.g. 'Merge PR #15 into main', 'Write prompts for T2, T3, T5'), "
                   "never as shell commands. Keep it updated as the plan changes. Skip this for one-step requests.")
    tmp = f"{state_file}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh: json.dump(st, fh)
    os.replace(tmp, state_file)
    if msg: print(json.dumps({"hookSpecificOutput": {"hookEventName": name, "additionalContext": msg}}))

if __name__ == "__main__":
    try: main()
    except Exception: pass      # never get in the session's way
