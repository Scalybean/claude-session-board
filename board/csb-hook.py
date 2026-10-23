#!/usr/bin/env python3
"""Session board - state hook for macOS, Linux and WSL (the Windows counterpart is hook.ps1; both write the same files).
Called by Claude Code hooks. Reads the hook event JSON from stdin, records the session's state in
sessions/<session_id>.json, then rebuilds sessions.js for the web dashboard. Never fails loudly: a hook must not
get in the way of the session it watches."""
import glob, json, os, re, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from csb_paths import find_dir

DAY_MS = 86400000
TL_ASST = re.compile(r'"type":\s*"text",\s*"text":\s*"(?P<text>(?:[^"\\]|\\.){0,900})'
                     r'|"type":\s*"tool_use",\s*"id":\s*"[^"]*",\s*"name":\s*"(?P<name>[^"]+)",\s*"input":\s*\{(?P<input>.{0,700})')
TL_ACTS = re.compile(r"^(Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell|WebSearch|WebFetch|Agent|Task|Skill)$")

def now_ms():
    return int(time.time() * 1000)

def clip(s, n):
    if not s: return ""
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s[:n - 1] + "…" if len(s) > n else s

def write_atomic(path, text):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", errors="replace", newline="") as f: f.write(text)
    os.replace(tmp, path)

def dumps(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))

def clean_command(c):
    """show the command that matters, not the "cd <folder> &&" or "VAR=value;" in front of it"""
    c = c or ""
    prev = None
    while prev != c:
        prev = c
        c = re.sub(r'^\s*(cd|pushd|Set-Location)\s+("[^"]*"|\'[^\']*\'|\S+)\s*(&&|;)\s*', "", c)
        c = re.sub(r'^\s*(export\s+)?[A-Za-z_][A-Za-z0-9_]*=("[^"]*"|\'[^\']*\'|\S*)\s*(&&|;)\s*', "", c)
    return c

def describe_tool(name, inp):
    inp = inp if isinstance(inp, dict) else {}
    if not inp: return name
    if name in ("Edit", "MultiEdit", "Write", "Read", "NotebookEdit"):
        f = inp.get("file_path") or inp.get("notebook_path")
        if f: return f"{name} " + re.split(r"[\\/]", f)[-1]
    elif name in ("Bash", "PowerShell"): return "Ran " + clip(clean_command(inp.get("command")), 70)
    elif name in ("Grep", "Glob"): return f"{name} " + clip(inp.get("pattern"), 50)
    elif name == "WebFetch": return "Fetched " + clip(inp.get("url"), 60)
    elif name == "WebSearch": return "Searched " + clip(inp.get("query"), 60)
    elif name in ("Task", "Agent"): return "Subagent: " + clip(inp.get("description"), 60)
    return name

# ---------- which process is Claude, and is it still alive ----------
def ps_row(pid):
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    m = re.match(r"\s*(\d+)\s+(.*\S)", out)
    return (int(m.group(1)), os.path.basename(m.group(2))) if m else None

def start_ms(pid):
    """when the process started (epoch ms), from ps's elapsed time; None if it is gone"""
    try:
        out = subprocess.run(["ps", "-o", "etime=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return None
    m = re.match(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)$", out)
    if not m: return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return now_ms() - ((d * 24 + h) * 3600 + mi * 60 + s) * 1000

def find_claude_process():
    cur = os.getpid()
    for _ in range(8):
        row = ps_row(cur)
        if not row: return None
        ppid, _ = row
        parent = ps_row(ppid)
        if not parent: return None
        if re.match(r"^(claude|node)$", parent[1]):
            st = start_ms(ppid)
            return {"pid": ppid, "started": st} if st else {"pid": ppid, "started": None}
        cur = ppid
    return None

def pid_alive(pid, started):
    try: os.kill(int(pid), 0)
    except ProcessLookupError: return False
    except PermissionError: return True
    except Exception: return True
    if started:
        st = start_ms(pid)
        if st is not None and abs(st - int(started)) > 5000 + 2000: return False      # etime is whole seconds
    return True

# ---------- transcript timeline ----------
def unescape_lite(s):
    """decode JSON string escapes in one pass, tolerant of text cut off in the middle"""
    if not s: return ""
    def one(m):
        c = m.group(1)
        if len(c) == 5: return chr(int(c[1:], 16))
        return {"n": "\n", "t": " ", "r": "", "b": "", "f": ""}.get(c, c)
    out = re.sub(r"\\(u[0-9a-fA-F]{4}|.)", one, s, flags=re.S)
    return out.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")      # pairs merge, lone halves become U+FFFD

def field(frag, key):
    m = re.search('"' + key + r'":\s*"((?:[^"\\]|\\.)*)', frag)
    return unescape_lite(m.group(1)) if m else None

def first_sentences(s, n):
    if not s: return ""
    s = s.split("\n\n")[0]
    s = re.sub(r"\*\*|`", "", s)
    return clip(s, n)

def iso_ms(text):
    from datetime import datetime
    try: return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception: return 0

def line_items(line, is_asst):
    out = []
    m = re.search(r'"timestamp":\s*"([^"]+)"', line)
    ts = iso_ms(m.group(1)) if m else 0
    if not is_asst:
        m = re.search(r'"role":\s*"user",\s*"content":\s*"((?:[^"\\]|\\.){0,600})', line)
        if m:
            txt = unescape_lite(m.group(1))
            if txt and not re.match(r"^\s*<", txt): out.append({"t": ts, "k": "you", "x": clip(txt, 140)})
        return out
    for m in TL_ASST.finditer(line):
        if m.group("text") is not None:
            x = first_sentences(unescape_lite(m.group("text")), 200)
            if x: out.append({"t": ts, "k": "say", "x": x})
        elif TL_ACTS.match(m.group("name") or ""):
            name, frag = m.group("name"), m.group("input")
            x = describe_tool(name, {k: field(frag, k) for k in ("file_path", "notebook_path", "command", "description", "query", "url")})
            sk = field(frag, "skill")
            if name == "Skill" and sk: x = "Skill " + sk
            out.append({"t": ts, "k": "act", "x": x})
    return out

def read_timeline(path):
    """Claude's own short updates, its key actions and your requests, from the end of the session transcript"""
    res = {"items": [], "title": None}
    if not path or not os.path.isfile(path): return res
    try:
        with open(path, "rb") as f:
            total = f.seek(0, 2); take = min(total, 4194304); f.seek(total - take)
            buf = f.read(take)
    except OSError:
        return res
    lines = buf.decode("utf-8", errors="replace").split("\n")
    first = 1 if take < total else 0                       # first line may be cut in half
    newest = []
    for i in range(len(lines) - 1, first - 1, -1):
        line = lines[i]
        if not res["title"]:
            m = re.search(r'"aiTitle":\s*"((?:[^"\\]|\\.)*)"', line)
            if m: res["title"] = unescape_lite(m.group(1)); continue
        if len(newest) >= 8:
            if res["title"]: break
            continue
        if not re.search(r'"isSidechain":\s*false', line): continue      # skip subagents' own transcripts
        is_asst = bool(re.search(r'"role":\s*"assistant"', line))
        if not is_asst and not re.search(r'"role":\s*"user",\s*"content":\s*"', line): continue
        for it in reversed(line_items(line, is_asst)):
            if newest and newest[-1]["x"] == it["x"]: continue
            newest.append(it)
    newest.reverse()
    res["items"] = newest[-8:]
    return res

def update_tasks(s, ev):
    """keep the session's task list, from whichever task tool Claude Code uses"""
    name = str(ev.get("tool_name") or ""); inp = ev.get("tool_input") or {}
    tasks = [t for t in (s.get("tasks") or []) if t]
    if name == "TodoWrite" and inp.get("todos"):
        tasks = [{"id": str(i), "subject": clip(td.get("content"), 120), "active": clip(td.get("activeForm"), 120), "status": str(td.get("status") or "")}
                 for i, td in enumerate(inp["todos"], 1)]
    elif name == "TaskCreate":
        resp = json.dumps(ev.get("tool_response"), ensure_ascii=False)
        m = re.search(r"#(\d+)", resp) or re.search(r'"id"\s*:\s*"?(\d+)', resp)
        tasks.append({"id": m.group(1) if m else str(len(tasks) + 1), "subject": clip(inp.get("subject"), 120),
                      "active": clip(inp.get("activeForm"), 120), "status": "pending"})
    elif name == "TaskUpdate" and inp.get("taskId"):
        for t in list(tasks):
            if str(t.get("id")) == str(inp["taskId"]):
                if inp.get("status") == "deleted": tasks.remove(t); break
                if inp.get("status"): t["status"] = str(inp["status"])
                if inp.get("subject"): t["subject"] = clip(inp["subject"], 120)
                if inp.get("activeForm"): t["active"] = clip(inp["activeForm"], 120)
    else:
        return
    s["tasks"] = tasks

# ---------- the state machine (same rules as hook.ps1) ----------
def apply_event(s, ev, t0):
    name = str(ev.get("hook_event_name") or "")
    new_state = detail = None
    if name == "SessionStart":
        new_state, detail = "ready", "Waiting for your first message"
        if ev.get("source") == "resume": detail = "Resumed, waiting for you"
        if ev.get("source") == "compact": new_state = None
        if ev.get("model"): s["model"] = str(ev["model"])
        s["endedAt"] = None
        if not os.environ.get("WSL_DISTRO_NAME"):          # under WSL the board may be shared with Windows sessions: a WSL pid means nothing there
            p = find_claude_process()
            if p: s["pid"], s["pidStarted"], s["term"] = p["pid"], p["started"], f"unix:{p['pid']}"
    elif name == "UserPromptSubmit":
        new_state, detail = "working", "Thinking"
        prompt = clip(ev.get("prompt"), 160)
        if re.match(r"^<[A-Za-z_-]+>", prompt): prompt = ""      # injected by Claude Code itself, not your request
        if prompt:
            s["lastPrompt"] = prompt
            if not s.get("firstPrompt"): s["firstPrompt"] = prompt
            if len(prompt) >= 25 or not s.get("topic"): s["topic"] = prompt
    elif name == "PostToolUse":
        new_state, detail = "working", describe_tool(ev.get("tool_name"), ev.get("tool_input"))
    elif name == "PostToolUseFailure":
        new_state, detail = "working", describe_tool(ev.get("tool_name"), ev.get("tool_input")) + " (failed)"
    elif name == "PermissionRequest":
        new_state = "needs"; tn = str(ev.get("tool_name") or ""); ti = ev.get("tool_input") or {}
        if tn == "AskUserQuestion":
            qs = ti.get("questions") or [{}]; q = str((qs[0] or {}).get("question") or "")
            detail = "Asks: " + clip(q, 140) if q else "Asked you a question"
        elif tn in ("Bash", "PowerShell") and ti.get("command"): detail = "Permission to run: " + clip(ti["command"], 120)
        else: detail = "Permission to use " + describe_tool(tn, ti)
    elif name == "Notification":
        nt = str(ev.get("notification_type") or ""); msg = clip(ev.get("message"), 160)
        if re.match(r"^(permission_prompt|elicitation_dialog)$", nt):
            new_state, detail = "needs", msg or "Waiting for your answer"
            if s.get("state") == "needs" and str(s.get("detail") or "").startswith("Permission ") and nt == "permission_prompt": detail = s["detail"]
    elif name == "Stop":
        new_state, detail = "yourturn", "Finished, your turn"
        last = clip(ev.get("last_assistant_message"), 220)
        if last: s["lastReply"] = last
    elif name == "StopFailure":
        new_state, detail = "error", clip("Stopped with an error " + str(ev.get("error") or ""), 160)
    elif name == "ChatReport":                                  # sent by the desktop chat add-on, not by Claude Code
        s["surface"] = "chat"
        if ev.get("title"): s["name"] = clip(ev["title"], 80)
        if ev.get("steps"):
            m = {"done": "completed", "in_progress": "in_progress", "not_started": "pending", "stopped": "pending"}
            s["tasks"] = [{"id": str(i), "subject": clip(st.get("title"), 120), "active": "", "status": m.get(str(st.get("status")), "pending")}
                          for i, st in enumerate(ev["steps"], 1)]
        sm = clip(ev.get("summary"), 200)
        if sm: s["timeline"] = [{"t": t0, "k": "say", "x": sm}]
        if str(ev.get("chat_state")) == "working": new_state, detail = "working", sm or "Working"
        else:
            new_state, detail = "yourturn", "Finished, your turn"
            if sm: s["lastReply"] = sm
    elif name == "SessionEnd":
        new_state, detail = "ended", "Session closed"; s["endedAt"] = t0
    return name, new_state, detail

def handle(ev):
    base = find_dir(); sess_dir = os.path.join(base, "sessions"); os.makedirs(sess_dir, exist_ok=True)
    t0 = now_ms(); sid = str(ev["session_id"]); path = os.path.join(sess_dir, sid + ".json")
    try:
        with open(path, encoding="utf-8") as f: s = json.load(f)
    except Exception:
        s = None
    if not s: s = {"id": sid, "createdAt": t0, "state": "ready", "t": 0, "events": 0}
    is_newer = t0 >= int(s.get("t") or 0)               # async hooks can finish out of order
    s["cwd"] = str(ev.get("cwd") or "")
    s["lastEventAt"] = max(int(s.get("lastEventAt") or 0), t0)
    s["events"] = int(s.get("events") or 0) + 1
    if ev.get("session_title"): s["name"] = clip(ev["session_title"], 80)
    if ev.get("permission_mode"): s["mode"] = str(ev["permission_mode"])
    if os.environ.get("CSB_WSL_PID"): s["term"] = f"wsl:{os.environ.get('WSL_DISTRO_NAME', '')}:{os.environ['CSB_WSL_PID']}"
    name, new_state, detail = apply_event(s, ev, t0)
    if re.match(r"^PostToolUse", name) and str(ev.get("tool_name")) in ("TodoWrite", "TaskCreate", "TaskUpdate"): update_tasks(s, ev)
    if ev.get("transcript_path") and re.match(r"^(PostToolUse|UserPromptSubmit|Stop|PermissionRequest|Notification|SessionStart)$", name):
        if name != "PostToolUse" or t0 - int(s.get("tlAt") or 0) > 3000:    # at most every 3 s per session; Stop always refreshes
            tl = read_timeline(str(ev["transcript_path"]))
            if tl["items"]: s["timeline"] = tl["items"]
            if tl["title"]: s["aiTitle"] = clip(tl["title"], 80)
            s["tlAt"] = t0
    if s.get("state") == "ended" and name not in ("SessionStart", "UserPromptSubmit"): new_state = None
    if new_state and is_newer:
        if s.get("state") != new_state or (new_state == "yourturn" and name in ("Stop", "ChatReport")): s["stateSince"] = t0
        s["state"], s["detail"], s["t"] = new_state, detail, t0
    elif new_state == "working" and s.get("state") == "working" and detail:
        s["detail"] = detail
    if not s.get("stateSince"): s["stateSince"] = t0
    write_atomic(path, dumps(s))

def rebuild(base):
    """sessions.js for the web dashboard: all sessions, dead ones marked ended, old ended ones forgotten"""
    sess_dir = os.path.join(base, "sessions"); now = now_ms(); out = []
    for f in sorted(glob.glob(os.path.join(sess_dir, "*.json"))):
        try:
            with open(f, encoding="utf-8") as fh: o = json.load(fh)
        except Exception:
            continue
        if not o: continue
        if o.get("pid") and o.get("state") != "ended" and str(o.get("term") or "").startswith("unix:") and not pid_alive(o["pid"], o.get("pidStarted")):
            o.update(state="ended", detail="Terminal closed", endedAt=now, stateSince=now); write_atomic(f, dumps(o))
        if o.get("state") == "ended" and o.get("endedAt") and now - int(o["endedAt"]) > 2 * DAY_MS:
            try: os.remove(f)
            except OSError: pass
            continue
        out.append(o)
    latest = {}                                          # one terminal runs one session at a time
    for o in out:
        if o.get("term") and o.get("state") != "ended":
            k = str(o["term"])
            if k not in latest or int(o.get("lastEventAt") or 0) > int(latest[k].get("lastEventAt") or 0): latest[k] = o
    for o in out:
        if o.get("term") and o.get("state") != "ended" and latest[str(o["term"])] is not o:
            o.update(state="ended", detail="Replaced by a newer session in the same terminal", endedAt=now, stateSince=now)
            write_atomic(os.path.join(sess_dir, str(o["id"]) + ".json"), dumps(o))
    try: page_at = int(os.path.getmtime(os.path.join(base, "dashboard.html")) * 1e7)
    except OSError: page_at = 0
    write_atomic(os.path.join(base, "sessions.js"), "window.__CSB__ = { generatedAt: %d, pageAt: %d, sessions: %s };" % (now, page_at, dumps(out)))

class Lock:
    """one writer at a time per name (flock); on platforms without it, no lock"""
    def __init__(self, base, name, wait):
        self.path, self.wait, self.f = os.path.join(base, ".locks", name), wait, None
    def __enter__(self):
        try:
            import fcntl
            os.makedirs(os.path.dirname(self.path), exist_ok=True); self.f = open(self.path, "w")
            end = time.time() + self.wait
            while True:
                try: fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB); break
                except OSError:
                    if time.time() > end: break
                    time.sleep(0.05)
        except Exception:
            pass
        return self
    def __exit__(self, *a):
        if self.f: self.f.close()

def main():
    try:
        ev = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace") or "null")
    except Exception:
        return
    if not isinstance(ev, dict) or not ev.get("session_id") or ev.get("agent_id"): return    # subagent events: the parent covers them
    base = find_dir(); os.makedirs(base, exist_ok=True)
    with Lock(base, "s_" + re.sub(r"[^A-Za-z0-9_-]", "_", str(ev["session_id"])), 5): handle(ev)
    with Lock(base, "rebuild", 4): rebuild(base)

if __name__ == "__main__":
    try: main()
    except Exception: pass
    sys.exit(0)
