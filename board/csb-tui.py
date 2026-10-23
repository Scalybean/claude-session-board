#!/usr/bin/env python3
"""Claude Sessions - terminal version of the dashboard.

Reads the same session files the hooks write, so it shows exactly what the web board shows.
Runs in WSL, Linux or Windows Python (3.8+), no packages needed.

    python3 csb-tui.py

Also raises a Windows toast (plus a terminal bell) when a session is blocked on you, errors, or
finishes a run longer than MIN_RUN_S. If it is still waiting after AWAY_S (you left your desk) it also
pushes to your phone through ntfy.sh - see ntfy.json next to this file. `python3 csb-tui.py --test` sends one test toast.

Keys: up/down or j/k select · enter opens the step list · c resume it in a new tab · f copy folder
      s start claude in a folder you pick (new Windows Terminal tab; elsewhere it copies the command) · a attach a background session, or go to the
      session's terminal tab / the Claude desktop app
      b move an exited terminal session to the background · h hide · u unhide all · e show/hide exited
      / search the sessions on the board (exited ones too once e shows them; esc clears) · q quit
"""
import glob, itertools, json, os, re, secrets, shlex, shutil, subprocess, sys, threading, time, unicodedata, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

QUIET_MIN = 15     # working but silent this long -> say so
STALE_MIN = 30     # quiet this much longer than the oldest working session -> fold away
TICK = 1.0         # seconds between refreshes
MIN_RUN_S = 120    # notify "your turn" only after a run at least this long
AWAY_S = 180       # still waiting this long -> push to the phone as well
GRACE_S = 8        # a prompt answered within this long (auto mode, quick click) never notifies

# ---------- where the data lives ----------
from csb_paths import find_dir, has_tabs, windows_homes

BASE = find_dir()
TABS = has_tabs()      # tab jumping, new tabs and the Claude-app focus need Windows Terminal + PowerShell
SESS = os.path.join(BASE, "sessions")
HIDDEN_FILE = os.path.join(BASE, "tui-hidden.json")

CLOUD = None   # CloudFeed, started by main()

def load_sessions():
    out = CLOUD.list() if CLOUD else []
    for f in glob.glob(os.path.join(SESS, "*.json")):
        try:
            with open(f, encoding="utf-8-sig") as fh:
                out.append(json.load(fh))
        except Exception:
            pass
    return out

def load_hidden():
    try:
        with open(HIDDEN_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}

def save_hidden(h):
    try:
        with open(HIDDEN_FILE, "w", encoding="utf-8") as fh:
            json.dump(h, fh)
    except Exception:
        pass

# ---------- notifications ----------
TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent('ToastText02')
$n = $x.GetElementsByTagName('text')
$n.Item(0).AppendChild($x.CreateTextNode($env:CSB_T)) > $null
$n.Item(1).AppendChild($x.CreateTextNode($env:CSB_B)) > $null
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Claude Sessions').Show([Windows.UI.Notifications.ToastNotification]::new($x))
"""

def toast_command(title, body):
    """(argv, env) that shows a desktop notification here, or None when this machine has no way to"""
    env = dict(os.environ, CSB_T=title, CSB_B=body[:200])
    if sys.platform == "darwin" and shutil.which("osascript"):
        return ["osascript", "-e", 'display notification (system attribute "CSB_B") with title (system attribute "CSB_T")'], env
    if TABS:
        if os.name != "nt": env["WSLENV"] = env.get("WSLENV", "") + ":CSB_T/u:CSB_B/u"
        return ["powershell" if os.name == "nt" else "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", TOAST_PS], env
    if shutil.which("notify-send"):
        return ["notify-send", "--app-name=Claude Sessions", title, body[:200]], env
    return None

def toast(title, body):
    cmd = toast_command(title, body)
    if cmd:
        try:
            subprocess.Popen(cmd[0], env=cmd[1], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)   # don't block the screen refresh
        except Exception:
            pass
    sys.stdout.write("\a")

def ntfy_config():
    """topic lives in ntfy.json, written by the board's setup only when you turn phone pushes on; without it nothing is sent.
    Subscribe to the topic in the ntfy app. Set "enabled": false to stop pushes. Pushes include what the session did or is asking (reply, command, progress). ntfy.sh topics are not private beyond being
    hard to guess, so set "detail": false to send only the project name and a generic headline."""
    f = os.path.join(BASE, "ntfy.json")
    try:
        with open(f, encoding="utf-8") as fh: return json.load(fh)
    except Exception:
        return {"enabled": False}

def push(cfg, title, body, urgent):
    def go():
        try:
            data = json.dumps({"topic": cfg["topic"], "title": title, "message": body,
                               "priority": 4 if urgent else 3, "tags": ["warning" if urgent else "white_check_mark"]}).encode("utf-8")
            req = urllib.request.Request(cfg.get("server", "https://ntfy.sh"), data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()

def summarize(s, st):
    """one specific line about why this session wants you: progress, what it did, or what it is asking"""
    tasks = s.get("tasks") or []
    if isinstance(tasks, dict): tasks = [tasks]
    done = sum(1 for t in tasks if t.get("status") == "completed")
    prog = f"{done}/{len(tasks)} steps · " if tasks else ""
    clean = lambda x, n: " ".join(str(x or "").split())[:n]
    detail = clean(s.get("detail"), 160)
    if st == "needs":
        if detail.startswith("Permission to run: "): detail = "Wants to run: " + detail[len("Permission to run: "):]
        elif detail.startswith("Permission to use "): detail = "Wants to use " + detail[len("Permission to use "):]
        return prog + (detail or "waiting for your answer")
    if st == "error": return prog + "Stopped with an error" + (": " + detail if detail else "")
    nxt = next((t for t in tasks if t.get("status") != "completed"), None)
    reply = clean(s.get("lastReply"), 200) or detail
    return prog + (reply or "finished") + (f" · next: {clean(nxt.get('subject'), 60)}" if nxt else "")

class CloudFeed:
    """Cloud sessions, reported by cloud/board-report.py (installed by the environment setup script) through the
    private ntfy board topic. Polled every 10 s; cached in cloud/feed.json so they survive a restart."""
    def __init__(self):
        self.cfg = ntfy_config(); self.sessions = {}; self.since = "12h"
        self.cache = os.path.join(BASE, "cloud", "feed.json")
        try:
            with open(self.cache, encoding="utf-8") as fh: c = json.load(fh)
            self.sessions, self.since = c.get("sessions", {}), c.get("since", "12h")
        except Exception:
            pass
        if self.cfg.get("board_topic"): threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                url = f"{self.cfg.get('server', 'https://ntfy.sh')}/{self.cfg['board_topic']}/json?poll=1&since={self.since}"
                with urllib.request.urlopen(url, timeout=15) as r: lines = r.read().decode("utf-8").splitlines()
                for ln in lines:
                    ev = json.loads(ln)
                    if ev.get("event") != "message": continue
                    self.since = ev["id"]
                    try: self.apply(json.loads(ev["message"]))
                    except Exception: pass
                if lines:
                    os.makedirs(os.path.dirname(self.cache), exist_ok=True)
                    with open(self.cache + ".tmp", "w", encoding="utf-8") as fh: json.dump({"since": self.since, "sessions": self.sessions}, fh)
                    os.replace(self.cache + ".tmp", self.cache)
            except Exception:
                pass
            time.sleep(10)

    def apply(self, p):
        if not p.get("url"): return     # helper processes inside a cloud VM report too, but only real sessions have a claude.ai link
        old = self.sessions.get(p["id"]) or {}
        if p.get("ts", 0) < old.get("lastEventAt", 0): return
        st = p["state"]; s = dict(old)
        s.update(id=p["id"], surface="cloud", cwd=p.get("repo") or "cloud", state=st, detail=p.get("detail") or "", lastEventAt=p["ts"], t=p["ts"])
        if p.get("title"): s["aiTitle"] = p["title"]
        if p.get("prompt") and not str(p["prompt"]).lstrip('"').startswith("<"):
            s["topic"] = p["prompt"]; s.setdefault("firstPrompt", p["prompt"])
        if str(s.get("detail", "")).lstrip('"').startswith("<"): s["detail"] = ""
        if p.get("reply"): s["lastReply"] = p["reply"]
        if p.get("url"): s["url"] = p["url"]
        if p.get("tasks"): s["tasks"] = [{"id": str(i + 1), "subject": t["s"], "active": t["s"], "status": t["st"]} for i, t in enumerate(p["tasks"])]
        if old.get("state") != st or st == "yourturn": s["stateSince"] = p["ts"]
        if st == "ended": s["endedAt"] = p["ts"]
        s.setdefault("createdAt", p["ts"])
        self.sessions[p["id"]] = s

    def list(self):
        now = time.time() * 1000; out = []
        for s in list(self.sessions.values()):
            if now - s.get("lastEventAt", 0) > 2 * 86400000: continue
            s = dict(s)
            if s.get("state") in ("working", "ready") and now - s.get("lastEventAt", 0) > 3600000:
                s["state"] = "ended"; s["detail"] = "No report for " + dur(now - s["lastEventAt"]) + " (check it in claude.ai)"
                s["endedAt"] = s["lastEventAt"]
            out.append(s)
        return out

class Loader:
    """reads the session files, refreshes metrics and runs the notifier once a second in the background,
    so key presses only redraw from memory"""
    def __init__(self, app, notifier):
        self.app, self.notifier, self.sessions = app, notifier, []
        self.tick()
        threading.Thread(target=self.loop, daemon=True).start()
    def tick(self):
        ss = load_sessions()
        try: self.app.refresh_metrics(ss)
        except Exception: pass
        self.sessions = ss
        if self.notifier:
            try: self.notifier.check(ss)
            except Exception: pass
    def loop(self):
        while True:
            time.sleep(TICK)
            try: self.tick()
            except Exception: pass

class AgentsFeed:
    """Background sessions as Claude Code itself reports them (`claude agents --json --all`, the documented interface).
    Their state is more reliable than the hook files, and it carries the short id that attach and reply need."""
    def __init__(self):
        self.by_session = {}
        threading.Thread(target=self.loop, daemon=True).start()
    def loop(self):
        while True:
            try:
                r = subprocess.run(["claude", "agents", "--json", "--all"], capture_output=True, text=True, timeout=30)
                self.by_session = {a["sessionId"]: a for a in json.loads(r.stdout or "[]") if a.get("sessionId")}
            except Exception:
                pass
            time.sleep(10)

class PRStatus:
    """open/merged/closed and checks for PR links found in session transcripts, via gh, refreshed every 2 minutes"""
    def __init__(self):
        self.info = {}; self.want = set()
        threading.Thread(target=self.loop, daemon=True).start()
    def get(self, url):
        self.want.add(url); return self.info.get(url)
    def loop(self):
        while True:
            for url in list(self.want):
                try:
                    r = subprocess.run(["gh", "pr", "view", url, "--json", "number,state,isDraft,reviewDecision,statusCheckRollup"], capture_output=True, text=True, timeout=30)
                    d = json.loads(r.stdout)
                    checks = [c.get("conclusion") or c.get("state") or c.get("status") for c in d.get("statusCheckRollup") or []]
                    bad = any(str(c).upper() in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT") for c in checks)
                    wait = any(str(c).upper() in ("PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED") for c in checks)
                    st = "draft" if d.get("isDraft") and d["state"] == "OPEN" else d["state"].lower()
                    self.info[url] = {"n": d["number"], "state": st, "checks": "failing" if bad else "running" if wait else "passing" if checks else "",
                                      "review": (d.get("reviewDecision") or "").lower().replace("_", " ")}
                except Exception:
                    pass
            time.sleep(120)

class Overlaps:
    """live sessions that share a git working tree (or a branch of one repo) with another live session, every 15 s in the
    background: git on the Windows drive is slow from WSL, so never in the frame. by_id[sid] = (kind, [other titles])"""
    def __init__(self):
        self.by_id = {}; self.sessions = []
        threading.Thread(target=self.loop, daemon=True).start()
    def loop(self):
        import csb_session
        while True:
            try:
                live = [s for s in self.sessions if s.get("cwd") and s.get("surface") not in ("chat", "cloud")]
                info = {s.get("id"): csb_session.tree_info(s["cwd"]) for s in live}
                out = {}
                for a in live:
                    for b in live:
                        if a is b: continue
                        kind = csb_session.overlap(info.get(a.get("id")), info.get(b.get("id")))
                        if not kind: continue
                        k, names = out.get(a.get("id"), (kind, []))
                        out[a.get("id")] = ("tree" if "tree" in (k, kind) else "branch", names + [csb_session.title(b)])
                self.by_id = out
            except Exception:
                pass
            time.sleep(15 if self.sessions else 0.5)      # the first frame hasn't handed over the sessions yet

class Notifier:
    """Toast when a session newly needs you. Sessions already waiting at startup stay quiet."""
    def __init__(self):
        self.seen = {s.get("id"): (s.get("state"), s.get("stateSince")) for s in load_sessions()}
        self.pending = {}; self.worked = {}; self.waiting = {}   # waiting: id -> (key, since, title, headline, urgent)
        self.cfg = ntfy_config()

    def check(self, sessions=None):
        now = time.time()
        for s in (sessions if sessions is not None else load_sessions()):
            i, st = s.get("id"), s.get("state")
            if st == "working": self.worked.setdefault(i, now)
            key = (st, s.get("stateSince"))
            head = summarize(s, st) if st in ("needs", "error", "yourturn") else None
            if not head:
                self.pending.pop(i, None); self.waiting.pop(i, None); self.seen[i] = key; continue
            if key == self.seen.get(i): continue
            if i not in self.pending or self.pending[i][0] != key: self.pending[i] = (key, now)
            if now - self.pending[i][1] < GRACE_S: continue
            self.seen[i] = key; self.pending.pop(i, None)
            if st == "yourturn" and now - self.worked.get(i, now) < MIN_RUN_S: continue   # short turn: you are at the keyboard
            self.worked.pop(i, None)
            t = s.get("name") or s.get("aiTitle") or s.get("topic") or s.get("firstPrompt") or "session"
            t = {"needs": "Needs you", "error": "Error", "yourturn": "Done"}[st] + " · " + base(s.get("cwd")) + " · " + " ".join(str(t).split())[:50]
            if st != "yourturn": toast(t, head)     # finished work doesn't interrupt: it waits in the review queue
            generic = {"needs": "Needs you", "error": "Stopped with an error", "yourturn": "Finished, your turn"}[st]
            if st != "yourturn":   # "done" doesn't buzz the phone: finished work waits in the review queue
                self.waiting[i] = (key, now, t if self.cfg.get("detail", True) else generic, head if self.cfg.get("detail", True) else "", True)
        for i, (key, since, t, head, urgent) in list(self.waiting.items()):
            if now - since >= AWAY_S and self.cfg.get("enabled", True) and self.cfg.get("topic"):
                push(self.cfg, t, head, urgent); self.waiting.pop(i)

# ---------- transcript metrics: tokens, plan steps, subagents, plan usage ----------
def project_roots():
    roots = [os.path.expanduser("~/.claude/projects")] + [os.path.join(h, ".claude", "projects") for h in windows_homes() if os.path.isdir(os.path.join(h, ".claude", "projects"))]
    return [r for r in roots if os.path.isdir(r)]

def fmt_n(n):
    n = int(n or 0)
    return f"{n/1e6:.1f}M" if n >= 1e6 else f"{n/1e3:.0f}k" if n >= 1e4 else f"{n/1e3:.1f}k" if n >= 1000 else str(n)

def parse_ts(t):
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0

class Tail:
    """One transcript, read incrementally: only bytes added since last time are parsed."""
    def __init__(self, path):
        self.path = path; self.lock = threading.Lock(); self.reset()
    def reset(self):
        self.off = 0; self.msgs = {}; self.ctx = 0; self.model = ""; self.tasks = []; self.first = 0; self.prs = []
    def feed(self):
        with self.lock:
            try: size = os.path.getsize(self.path)
            except OSError: return
            if size < self.off: self.reset()
            if size == self.off: return
            with open(self.path, "rb") as fh:
                fh.seek(self.off); data = fh.read(size - self.off)
            cut = data.rfind(b"\n")
            if cut < 0: return
            self.off += cut + 1
            for pr in re.findall(rb"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+", data[:cut]):
                u = pr.decode()
                if u in self.prs: self.prs.remove(u)
                self.prs.append(u)
            for line in data[:cut].split(b"\n"):
                if b'"usage"' not in line and b'"Todo' not in line and b'"Task' not in line: continue
                try: d = json.loads(line)
                except Exception: continue
                if d.get("type") != "assistant" or not isinstance(d.get("message"), dict): continue
                m = d["message"]; u = m.get("usage")
                if u:
                    ins = (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
                    ts = parse_ts(d.get("timestamp"))
                    self.msgs[m.get("id") or d.get("uuid")] = (ins, u.get("output_tokens") or 0, ts)
                    self.ctx = ins + (u.get("cache_read_input_tokens") or 0)
                    if m.get("model") and not str(m["model"]).startswith("<"): self.model = m["model"]
                    if not self.first: self.first = ts
                for b in (m.get("content") or []):
                    if isinstance(b, dict) and b.get("type") == "tool_use": self.tool(b.get("name"), b.get("input") or {})
    def tool(self, name, inp):
        if name == "TodoWrite":
            self.tasks = [{"id": str(i + 1), "subject": t.get("content", ""), "active": t.get("activeForm", ""), "status": t.get("status", "pending")}
                          for i, t in enumerate(inp.get("todos") or [])]
        elif name == "TaskCreate":
            self.tasks.append({"id": str(len(self.tasks) + 1), "subject": inp.get("subject", ""), "active": inp.get("activeForm", ""), "status": "pending"})
        elif name == "TaskUpdate":
            for t in self.tasks:
                if t["id"] == str(inp.get("taskId")):
                    if inp.get("status") == "deleted": self.tasks.remove(t)
                    else:
                        if inp.get("status"): t["status"] = inp["status"]
                        if inp.get("subject"): t["subject"] = inp["subject"]
                    break
    def totals(self):
        with self.lock:
            return sum(v[0] for v in self.msgs.values()), sum(v[1] for v in self.msgs.values())

class Metrics:
    def __init__(self):
        self.tails = {}; self.paths = {}; self.lock = threading.Lock(); self.window = {}; self.rate = None
        threading.Thread(target=self.scan_loop, daemon=True).start()
    def tail(self, path):
        with self.lock:
            t = self.tails.get(path)
            if not t: t = self.tails[path] = Tail(path)
            return t
    def transcript(self, sid):
        p = self.paths.get(sid)
        if p and os.path.exists(p): return p
        if p is None and sid in self.paths: return None
        if time.time() - getattr(self, "miss_at", {}).get(sid, 0) < 60: return None
        self.__dict__.setdefault("miss_at", {})[sid] = time.time()
        for r in project_roots():
            hit = glob.glob(os.path.join(r, "*", sid + ".jsonl"))
            if hit: self.paths[sid] = hit[0]; return hit[0]
        return None
    def session(self, sid):
        """tokens, context, derived plan steps and subagents for one session"""
        p = self.transcript(sid)
        if not p: return None
        t = self.tail(p); t.feed()
        ins, out = t.totals()
        subs = []; sub_tok = 0
        for meta in glob.glob(os.path.join(p[:-6], "subagents", "agent-*.meta.json")):
            try:
                with open(meta, encoding="utf-8") as fh: mj = json.load(fh)
                jl = meta[:-len(".meta.json")] + ".jsonl"
                st = self.tail(jl); st.feed(); si, so = st.totals(); sub_tok += si + so
                alive = not mj.get("finished") and time.time() - os.path.getmtime(jl) < 600
                subs.append({"what": mj.get("description") or mj.get("agentType") or "subagent", "running": alive, "tok": si + so})
            except Exception:
                pass
        try: fresh = time.time() - os.path.getmtime(p)
        except OSError: fresh = 1e9
        return {"prs": list(t.prs[-3:]), "in": ins, "out": out, "ctx": t.ctx, "model": t.model, "tasks": t.tasks, "subs": subs, "sub_tok": sub_tok, "fresh": fresh, "first": t.first}
    def scan_loop(self):
        """every minute: tokens in the last 5 h and 7 d over all recent transcripts, and the saved plan usage"""
        while True:
            try:
                now = time.time(); w5 = w7 = 0
                for r in project_roots():
                    for f in glob.glob(os.path.join(r, "**", "*.jsonl"), recursive=True):
                        try:
                            if now - os.path.getmtime(f) > 7 * 86400: continue
                        except OSError: continue
                        t = self.tail(f); t.feed()
                        with t.lock:
                            for i, o, ts in t.msgs.values():
                                if now - ts <= 5 * 3600: w5 += i + o
                                if now - ts <= 7 * 86400: w7 += i + o
                self.window = {"5h": w5, "7d": w7}
            except Exception:
                pass
            try:
                with open(os.path.join(BASE, "usage.json"), encoding="utf-8") as fh: self.rate = json.load(fh)
            except Exception:
                self.rate = None
            time.sleep(60)

def bar(frac, n=10):
    k = max(0, min(n, int(round(frac * n))))
    return "█" * k + "░" * (n - k)

_MEMO = {}
def memo(ttl):
    """cache a function's result per argument for ttl seconds: these read files on the Windows drive, which is slow from WSL"""
    def wrap(fn):
        def inner(*a):
            k = (fn.__name__,) + a; hit = _MEMO.get(k)
            if hit and time.time() - hit[0] < ttl: return hit[1]
            v = fn(*a); _MEMO[k] = (time.time(), v); return v
        return inner
    return wrap

@memo(10)
def repo_features(cwd):
    """features passing / total from tasks/FEATURES.json in the project (or its worktree origin)"""
    try:
        with open(os.path.join(cwd, "tasks", "FEATURES.json"), encoding="utf-8") as fh: fs = json.load(fh)
        fs = fs.get("features", fs) if isinstance(fs, dict) else fs
        return sum(1 for f in fs if f.get("passes")), len(fs)
    except Exception:
        return None

@memo(3)
def plan_file_tasks(sid):
    """steps from plans/<session>.md, written by sessions that have no task tool: '- [ ] todo', '- [>] doing', '- [x] done'"""
    try:
        with open(os.path.join(BASE, "plans", sid + ".md"), encoding="utf-8") as fh: txt = fh.read()
    except Exception:
        return []
    out = []
    for i, (mk, text) in enumerate(re.findall(r"^\s*[-*]\s*\[([ xX>~])\]\s*(.+)$", txt, re.M)):
        st = "completed" if mk in "xX" else "in_progress" if mk in ">~" else "pending"
        out.append({"id": str(i + 1), "subject": text.strip(), "active": text.strip(), "status": st})
    return out

@memo(10)
def repo_packets(cwd):
    """open task packets (tasks/<slug>.md with a status: line) -> [(slug, status, goal)]"""
    out = []
    for f in sorted(glob.glob(os.path.join(cwd, "tasks", "*.md"))):
        slug = os.path.basename(f)[:-3]
        if slug.upper() == "PROGRESS": continue
        try:
            with open(f, encoding="utf-8") as fh: txt = fh.read(3000)
        except Exception:
            continue
        st = re.search(r"^status:\s*(\S+)", txt, re.M); goal = re.search(r"^goal:\s*(.+)", txt, re.M)
        if st and st.group(1) != "done": out.append((slug, st.group(1), goal.group(1).strip() if goal else ""))
    return out

# ---------- text helpers ----------
def cw(ch):
    if unicodedata.combining(ch): return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1

def width(s): return sum(cw(c) for c in s)

def is_rtl(c): return "\u0590" <= c <= "\u08ff" or "\ufb1d" <= c <= "\ufdff" or "\ufe70" <= c <= "\ufeff"
def is_ltr(c): return c.isalnum() and not is_rtl(c)
MIRROR = str.maketrans("()[]{}<>", ")(][}{><")

def direction(s):
    """R when the text starts (first letter) in a right-to-left script"""
    for c in str(s or ""):
        if is_rtl(c): return "R"
        if is_ltr(c): return "L"
    return "L"

def visual(s, base_=None):
    """Terminals print characters left to right, so Hebrew comes out backwards.
    Reorder each line into the order it should be seen (a small version of the Unicode bidi rules)."""
    if not any(is_rtl(c) for c in s): return s
    kinds = ["R" if is_rtl(c) else "L" if is_ltr(c) else "N" for c in s]
    base_ = base_ or next((k for k in kinds if k != "N"), "L")
    # a neutral (space, quote, dash) takes the direction around it, or the line's direction when the sides differ
    for i, k in enumerate(kinds):
        if k != "N": continue
        before = next((kinds[j] for j in range(i - 1, -1, -1) if kinds[j] != "N"), base_)
        after = next((kinds[j] for j in range(i + 1, len(kinds)) if kinds[j] != "N"), base_)
        kinds[i] = before if before == after else base_
    runs, cur, cur_k = [], "", None
    for c, k in zip(s, kinds):
        if k != cur_k and cur: runs.append((cur_k, cur)); cur = ""
        cur += c; cur_k = k
    if cur: runs.append((cur_k, cur))
    runs = [(k, x[::-1].translate(MIRROR) if k == "R" else x) for k, x in runs]
    if base_ == "R": runs.reverse()
    return "".join(x for _, x in runs)

def input_lines(text, n):
    """word-wrap typed text into lines of n columns, keeping line breaks and long words (split if needed)"""
    out = []
    for para in text.split("\n"):
        line = ""
        for w in para.split(" "):
            while width(w) > n:                       # a word longer than the line: split it
                room = n - width(line) - (1 if line else 0)
                if room < 5:
                    out.append(line); line = ""; room = n
                cut, used = "", 0
                for ch in w:
                    if used + cw(ch) > room: break
                    cut += ch; used += cw(ch)
                line = (line + " " + cut) if line else cut; out.append(line); line = ""; w = w[len(cut):]
            if width(line) + width(w) + (1 if line else 0) <= n: line = (line + " " + w) if line else w
            else: out.append(line); line = w
        out.append(line)
    return out or [""]

def clip(s, n):
    """cut to n columns, ending with an ellipsis when something was cut"""
    s = " ".join(str(s or "").split())
    if width(s) <= n: return s
    out, w = "", 0
    for c in s:
        if w + cw(c) > n - 1: break
        out += c; w += cw(c)
    return out + "…"

def fit(s, n, base_=None):
    """cut to n columns, put right-to-left text in display order, and pad to exactly n;
    a right-to-left paragraph is aligned to the right, the way it is read"""
    if n <= 0: return ""
    s = clip(s, n); base_ = base_ or direction(s)
    gap = " " * (n - width(s))
    return gap + visual(s, base_) if base_ == "R" else visual(s, base_) + gap

def wrap(s, n, lines):
    """word-wrap to at most `lines` lines of n columns"""
    words, out, cur, used = " ".join(str(s or "").split()).split(" "), [], "", 0
    for w in words:
        if width(cur) + width(w) + (1 if cur else 0) <= n:
            cur = (cur + " " + w) if cur else w; used += 1
        else:
            out.append(cur); cur = ""
            if len(out) == lines: break
            cur = w; used += 1
    if len(out) < lines and cur: out.append(cur); cur = ""
    if used < len(words) or cur:   # text left over: end the last line with an ellipsis
        out[-1] = clip(out[-1] + " " + " ".join(words[used:]), n)
    return out or [""]

def base(p):
    p = str(p or "").rstrip("\\/")
    return p.replace("\\", "/").split("/")[-1] or p or "?"

def dur(ms):
    s = max(0, int(ms // 1000))
    if s < 60: return f"{s}s"
    m = s // 60
    if m < 60: return f"{m}m"
    h = m // 60
    if h < 24: return f"{h}h{m % 60:02d}" if m % 60 else f"{h}h"
    return f"{h // 24}d"

# ---------- colours (ANSI) ----------
ESC = "\x1b["
RESET, BOLD, DIM, REV = ESC + "0m", ESC + "1m", ESC + "2m", ESC + "7m"
BLUE, GREEN, RED, GREY = ESC + "38;5;111m", ESC + "38;5;114m", ESC + "38;5;203m", ESC + "38;5;245m"
HOT = ESC + "48;5;178m" + ESC + "38;5;16m"      # amber row, dark text: blocked on you
SEL = ESC + "48;5;237m"
AMBER = ESC + "38;5;178m"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# ---------- what each session shows ----------
def bucket(s):
    if s.get("state") == "ended": return "ended"
    if s.get("state") == "needs": return "needs"
    if s.get("state") == "working": return "working"
    return "turn"

def state_label(s, b):
    if b == "needs": return ("? permission" if str(s.get("detail", "")).startswith("Permission") else "? question"), ""
    if b == "working": return "● working", BLUE
    if b == "ended": return ("– exited" if s.get("state") == "ended" else "– idle"), GREY
    if s.get("state") == "error": return "✗ error", RED
    if s.get("state") == "ready": return "○ new", GREEN
    return "✓ your turn", GREEN

def describe(s, b, now):
    chat = s.get("surface") == "chat"
    folder = "desktop chat" if chat else base(s.get("cwd"))
    wsl = (not chat) and str(s.get("cwd", "")).startswith("/")
    topic = s.get("topic") if s.get("topic") and not str(s.get("topic")).startswith("/") else ""
    title = s.get("name") or s.get("aiTitle") or topic or s.get("topic") or s.get("firstPrompt") or folder
    tasks = s.get("tasks") or []
    if isinstance(tasks, dict): tasks = [tasks]
    done = sum(1 for t in tasks if t.get("status") == "completed")
    cur = next((t for t in tasks if t.get("status") == "in_progress"), None)
    nxt = next((t for t in tasks if t.get("status") != "completed"), None)
    says = [x.get("x") for x in (s.get("timeline") or []) if isinstance(x, dict) and x.get("k") == "say"]
    detail = s.get("detail") or ""
    if b == "needs":
        sub = "$ " + detail[len("Permission to run: "):] if detail.startswith("Permission to run: ") else detail.replace("Permission to use ", "use ") or "Waiting for your answer"
    elif b == "working":
        sub = (cur.get("active") or cur.get("subject")) if cur else (says[-1] if says else detail or "thinking")
        if now - (s.get("lastEventAt") or 0) > QUIET_MIN * 60000: sub += " · silent " + dur(now - s["lastEventAt"])
    elif b == "turn":
        sub = ("next: " + nxt.get("subject", "")) if (tasks and nxt) else (s.get("lastReply") or detail)
    else:
        sub = detail if s.get("state") == "ended" else (s.get("lastReply") or detail)
    count = f"{done}/{len(tasks)} · " if tasks and b != "ended" else ""
    proj = folder + (" [wsl]" if wsl else "")
    conv = s.get("aiTitle") if s.get("aiTitle") and s.get("aiTitle") != title else ""
    return dict(title=title, sub=sub, count=count, proj=proj, conv=conv, tasks=tasks, chat=chat)

def matches(s, query):
    """every word of the query appears somewhere in the session: titles, prompts, replies, folder, steps"""
    words = query.casefold().split()
    if not words: return True
    parts = [s.get(k) for k in ("name", "aiTitle", "topic", "firstPrompt", "lastPrompt", "lastReply", "detail", "cwd", "bg", "id")]
    parts += [x.get("x") for x in s.get("timeline") or [] if isinstance(x, dict)]
    tasks = s.get("tasks") or []
    parts += [t.get("subject") for t in ([tasks] if isinstance(tasks, dict) else tasks) if isinstance(t, dict)]
    hay = " ".join(str(x) for x in parts if x).casefold()
    return all(w in hay for w in words)

def setup_choices():
    try:
        with open(os.path.join(BASE, "setup.json"), encoding="utf-8") as f: return json.load(f).get("choices") or {}
    except Exception:
        return {}

def ct_command():
    return shutil.which("ct") or os.path.expanduser("~/.local/bin/ct")

def tasks_enabled():
    """the Tasks tab is for Todoist and for the ct launcher's review queue; off until either was chosen (or already set up)"""
    c = setup_choices()
    return bool(c.get("todoist") or c.get("ct") or os.environ.get("TODOIST_TOKEN") or os.path.isfile(os.path.join(BASE, "todoist.json")))

# ---------- the app ----------
class App:
    def __init__(self):
        self.sel = None; self.open = set(); self.show_ended = False; self.top = 0; self.search = ""
        self.hidden = load_hidden(); self.toast = ""; self.toast_at = 0; self.order = []
        self.metrics = Metrics(); self.m = {}; self.m_at = 0
        self.typing = None; self.typing_repo = ""; self.reply_to = None; self.term_old = None; self.confirm_x = None; self.confirm_at = 0
        self.input_label = None; self.input_cb = None; self.input_empty = False
        self.view = "sessions"
        self.tasksv = None
        try:
            if not tasks_enabled(): raise RuntimeError("Todoist and ct are off (csb-tui.py --setup)")
            import tasks_view
            self.tasksv = tasks_view.TasksView(self, sys.modules[__name__])
        except Exception as e:
            self.tasksv = None; self.tasks_error = str(e)
        self.agents = AgentsFeed(); self.prs = PRStatus(); self.overlaps = Overlaps()
        self.picker = None      # the folder browser behind `s`
        self.help = None        # the key list behind `?`

    def refresh_metrics(self, all_):
        """per-session numbers from the transcripts; also fixes up headless `claude -p` runs, which only fire SessionStart/End hooks"""
        if time.time() - self.m_at < 2: return
        self.m_at = time.time()
        for s in all_:
            if s.get("state") == "ended" and (s.get("endedAt") or 0) < time.time() * 1000 - 600000 and s.get("id") in self.m: continue
            try: self.m[s.get("id")] = self.metrics.session(s.get("id"))
            except Exception: pass

    def groups(self, now):
        loader = getattr(self, "loader", None)
        all_ = [dict(x) for x in loader.sessions] if loader else load_sessions()
        # background sessions: Claude Code's own state wins over the hook files
        seen = set()
        for s in all_:
            a = self.agents.by_session.get(s.get("id"))
            if not a: continue
            seen.add(s.get("id")); s["bg"] = a.get("id")
            if a.get("kind") == "background" and a.get("state") and a.get("status"):     # live process only
                st = {"working": "working", "blocked": "needs", "done": "yourturn", "failed": "error", "stopped": "ended"}.get(a["state"])
                if st and st != s.get("state"):
                    s["state"] = st
                    if st == "needs": s["detail"] = "Waiting: " + (a.get("waitingFor") or "your answer")
        for sid, a in self.agents.by_session.items():     # a background session the hooks never saw
            if sid in seen or a.get("kind") != "background" or a.get("state") in ("stopped",) or not a.get("status"): continue
            st = {"working": "working", "blocked": "needs", "done": "yourturn", "failed": "error"}.get(a.get("state"), "yourturn")
            all_.append({"id": sid, "bg": a.get("id"), "cwd": a.get("cwd"), "name": a.get("name"), "state": st,
                         "detail": "Waiting: " + (a.get("waitingFor") or "your answer") if st == "needs" else "",
                         "lastEventAt": a.get("startedAt"), "stateSince": a.get("startedAt"), "createdAt": a.get("startedAt")})
        if not getattr(self, "loader", None): self.refresh_metrics(all_)
        for s in all_:
            m = self.m.get(s.get("id"))
            if m and s.get("state") == "ready" and m["fresh"] < 20:    # transcript is growing but no hook reported work: a headless run
                s["state"] = "working"; s["detail"] = "headless run"; s["stateSince"] = int((time.time() - m["fresh"]) * 1000)
        work_at = [s.get("lastEventAt") or 0 for s in all_ if s.get("state") == "working"]
        # nothing working: measure against the most recently active session instead
        live_at = [s.get("lastEventAt") or 0 for s in all_ if s.get("state") != "ended"]
        ref = min(work_at) if work_at else (max(live_at) if live_at else None)
        g = {"needs": [], "working": [], "turn": [], "ended": []}; hid = 0
        for s in all_:
            b = bucket(s)
            if b == "turn" and ref is not None and (s.get("lastEventAt") or 0) < ref - STALE_MIN * 60000: b = "ended"
            if b in ("turn", "ended") and self.hidden.get(s.get("id")) == (s.get("t") or 0): hid += 1; continue
            gone = self.hidden.get("x:" + str(s.get("id")))
            if gone is not None and (s.get("lastEventAt") or 0) <= gone: hid += 1; continue
            g[b].append(s)
        since = lambda s: s.get("stateSince") or 0
        g["needs"].sort(key=since); g["working"].sort(key=since)
        g["turn"].sort(key=since, reverse=True)
        g["ended"].sort(key=lambda s: s.get("endedAt") or s.get("lastEventAt") or 0, reverse=True)
        return g, hid

    def frame(self):
        now = int(time.time() * 1000)
        cols, rows = shutil.get_terminal_size((100, 30))
        cols = max(30, cols - 1)   # leave the last column empty: writing into it makes some terminals wrap
        if (cols, rows) != getattr(self, "size", None): self.size = (cols, rows); self.clear = True
        if self.picker:
            head, body, sel_span, keys = self.picker.render(cols)
            return self._compose(head, body, sel_span, keys, cols, rows, self.picker)
        if self.help:
            head, body, sel_span, keys = self.help.render(cols, rows)
            return self._compose(head, body, sel_span, keys, cols, rows, self.help)
        if self.view == "tasks" and self.tasksv:
            head, body, sel_span, keys = self.tasksv.render(cols, rows)
            return self._compose(head, body, sel_span, keys, cols, rows, self.tasksv)
        g, hid = self.groups(now)
        query = self.typing if self.input_label == "search" else self.search
        if query.strip():
            total = sum(len(v) for b, v in g.items() if b != "ended" or self.show_ended)
            for b in g: g[b] = [s for s in g[b] if matches(s, query)]
        self.order = [(s, "needs") for s in g["needs"]] + [(s, "working") for s in g["working"]] + [(s, "turn") for s in g["turn"]]
        if self.show_ended: self.order += [(s, "ended") for s in g["ended"]]
        ids = [s.get("id") for s, _ in self.order]
        if self.sel not in ids: self.sel = ids[0] if ids else None
        self.overlaps.sessions = [x for x in g["needs"] + g["working"] + g["turn"]]

        RW = max(8, min(30, (cols - 4) // 3))        # progress rail length
        n_need, n_work, n_turn = len(g["needs"]), len(g["working"]), len(g["turn"])
        left_plain = f" sessions   {n_need} needs you · {n_work} working · {n_turn} your turn"
        left_plain += "  · tasks (Tab)" if self.tasksv else ""
        left = (f" {BOLD}sessions{RESET}   " + (f"{AMBER}{n_need} needs you{RESET}" if n_need else f"{GREY}0 need you{RESET}")
                + f"{GREY} · {RESET}{BLUE}{n_work}{RESET}{GREY} working · {RESET}{GREEN}{n_turn}{RESET}{GREY} your turn{RESET}"
                + (f"{GREY}  · tasks (Tab){RESET}" if self.tasksv else ""))
        def to_review(s):
            if s.get("surface") == "chat": return False      # Desktop chats aren't code tasks
            ts = s.get("tasks") or (self.m.get(s.get("id")) or {}).get("tasks") or plan_file_tasks(s.get("id") or "")
            packets = repo_packets(s.get("cwd") or "") if s.get("surface") not in ("chat", "cloud") else []
            return any(st == "review" for _, st, _ in packets) or (ts and all(t.get("status") == "completed" for t in ts))
        n_rev = sum(1 for i in self.tasksv.dl.items() if i["state"] == "for_review") if self.tasksv else 0
        if n_rev:
            left_plain += f" · {n_rev} to test" + (" - test before starting more" if n_rev > 2 else "")
            left += (AMBER if n_rev > 2 else GREEN) + f" · {n_rev} to test" + (" - test before starting more" if n_rev > 2 else "") + RESET
        clock = time.strftime("%H:%M:%S")
        head = [left + (" " * max(2, cols - len(left_plain) - len(clock)) + GREY + clock + RESET if cols >= len(left_plain) + len(clock) + 2 else ""),
                GREY + " " + clip(self.usage_line(), cols - 2) + RESET, ""]
        if query.strip():
            found = len(self.order)
            where = "incl. exited" if self.show_ended else "e adds exited"
            head[-1] = " " + AMBER + "/ " + RESET + BOLD + fit(query.strip(), max(4, cols // 2), "L") + RESET + GREY + f"  {found} of {total} · {where} · esc clears" + RESET
            head.append("")
        body, sel_span = [], (0, 0)

        def details(s, d, b, m, tasks):
            """what the expanded view adds under a session"""
            ind = "   "; out = []
            if tasks:
                for t in tasks:
                    st = t.get("status"); cur = st == "in_progress" and b == "working"
                    mk = (GREEN + "✓ " + RESET) if st == "completed" else ((BLUE + "› " + RESET + BOLD) if cur else "  ")
                    text = (t.get("active") if cur and t.get("active") else t.get("subject")) or ""
                    out.append(ind + mk + ("" if st == "completed" or cur else GREY) + fit(text, cols - 8) + RESET)
            else:
                out.append(ind + GREY + "no step list for this session" + RESET)
            subs = m.get("subs") or []
            if subs: out.append("")
            for x in subs[:6]:
                out.append(ind + (BLUE + "sub ● " if x["running"] else GREY + "sub ✓ ") + RESET + GREY + fit(x["what"], cols - 22) + " " + fmt_n(x["tok"]).rjust(6) + RESET)
            tl = [x for x in (s.get("timeline") or []) if isinstance(x, dict) and x.get("k") in ("you", "say")][-3:]
            if tl: out.append("")
            for x in tl:
                out.append(ind + GREY + {"you": "you  ", "say": "said "}[x.get("k")] + RESET + fit(x.get("x", ""), cols - 10, "L"))
            facts = []
            if m.get("model"): facts.append(m["model"])
            if m: facts.append(f"in {fmt_n(m['in'])} · out {fmt_n(m['out'])}" + (f" · subagents {fmt_n(m['sub_tok'])}" if m["sub_tok"] else ""))
            if m.get("first"): facts.append("running " + dur(now - m["first"] * 1000))
            if facts: out += ["", ind + GREY + fit("  ·  ".join(facts), cols - 4) + RESET]
            ov = self.overlaps.by_id.get(s.get("id")) if b != "ended" else None
            if ov and ov[0] == "tree": out.append(ind + AMBER + fit("another live session edits this same working tree; give one of them its own worktree (claude --worktree <name>)", cols - 4) + RESET)
            if s.get("url"): out.append(ind + GREY + fit(s["url"], cols - 4) + RESET)
            for url in m.get("prs") or []:
                pi = self.prs.get(url) or {}
                out.append(ind + GREY + fit(url + (f"  {pi.get('state', '')}" + (f" · checks {pi['checks']}" if pi.get("checks") else "") + (f" · {pi['review']}" if pi.get("review") else "") if pi else ""), cols - 4) + RESET)
            if s.get("bg"): out.append(ind + GREY + "a attach · r reply  (in an attached session, Ctrl+Z comes back here)" + RESET)
            elif s.get("state") == "ended" and s.get("surface") not in ("chat", "cloud"): out.append(ind + GREY + "b continue it in the background (then a attach · r reply)" + RESET)
            elif s.get("surface") == "chat": out.append(ind + GREY + "a bring the Claude app to the front" + RESET)
            elif s.get("surface") != "cloud" and TABS: out.append(ind + GREY + "a go to its terminal tab" + RESET)
            for slug, st, goal in (repo_packets(s.get("cwd") or "") if not d["chat"] and s.get("surface") != "cloud" else [])[:3]:
                out.append(ind + (AMBER if st == "needs-human" else GREY) + fit(f"task {slug} · {st}" + (" · " + goal if goal else ""), cols - 4) + RESET)
            return out

        def add_session(s, b):
            nonlocal sel_span
            d = describe(s, b, now); label, colour = state_label(s, b); m = self.m.get(s.get("id")) or {}
            tasks = d["tasks"] or m.get("tasks") or plan_file_tasks(s.get("id") or "")
            if b in ("working", "needs") and tasks and all(t.get("status") == "completed" for t in tasks):
                tasks = []      # every step done but working again: that list was for the previous request
            is_sel = s.get("id") == self.sel; bg = SEL if is_sel else ""
            start = len(body)
            err = s.get("state") == "error"
            glyph, gcol = ("✗", RED) if err else {"needs": ("◆", AMBER), "working": ("●", BLUE), "turn": ("✓", GREEN), "ended": ("–", GREY)}[b]
            word = label.split(" ", 1)[1] if label[:1] in "?●✓○✗–" and " " in label else label
            if b == "needs": word = "needs you · " + word
            right = word + " · " + dur(now - (s.get("stateSince") or s.get("lastEventAt") or now))
            rcol = AMBER if b == "needs" else (RED if err else GREY)
            avail = max(10, cols - 5 - width(right))
            proj = clip(d["proj"].replace(" [wsl]", ""), max(6, avail // 2)); rest = " · " + fit(d["title"], max(1, avail - width(proj) - 3), "L")
            body.append(bg + ("▸" if is_sel else " ") + gcol + glyph + RESET + bg + " " + (GREY if b == "ended" else BOLD) + proj + RESET + bg
                        + (GREY if b == "ended" else "") + rest + RESET + bg + " " + rcol + right + RESET)
            if tasks:
                done = sum(1 for t in tasks if t.get("status") == "completed")
                frac = (done + sum(0.5 for t in tasks if t.get("status") == "in_progress")) / len(tasks)
                k = int(round(frac * RW)); fill = AMBER if b == "needs" else GREEN if b == "turn" else GREY if b == "ended" else BLUE
                rail = fill + "━" * k + GREY + "╌" * (RW - k); prog = f"{done}/{len(tasks)}"
            else:
                rail = GREY + "╌" * RW; prog = "no plan"
            cur = next((t for t in tasks if t.get("status") == "in_progress"), None)
            nxt = next((t for t in tasks if t.get("status") == "pending"), None)
            if b == "working" and cur: now_txt = cur.get("active") or cur.get("subject") or ""
            elif b == "turn" and tasks and nxt: now_txt = "next: " + (nxt.get("subject") or "")
            else: now_txt = d["sub"].split("\n")[0]
            room = max(4, cols - 4 - RW - 2 - len(prog) - 3)
            body.append(bg + "   " + rail + RESET + bg + GREY + "  " + prog + "   " + RESET + bg + (AMBER if b == "needs" else GREY) + fit(now_txt, room, "L") + RESET)
            small = []
            if m:
                small.append(f"ctx {fmt_n(m['ctx'])}  out {fmt_n(m['out'])}" + (f"  all {fmt_n(m['in'] + m['out'] + m['sub_tok'])}" if m["sub_tok"] else ""))
                if m["subs"]: small.append(f"sub {len([x for x in m['subs'] if x['running']])}/{len(m['subs'])}")
            cloud = s.get("surface") == "cloud"
            feat = repo_features(s.get("cwd") or "") if not d["chat"] and not cloud else None
            if feat: small.append(f"features {feat[0]}/{feat[1]}")
            for url in (m.get("prs") or [])[-1:]:
                pi = self.prs.get(url)
                small.append(f"PR #{pi['n']} {pi['state']}" + (f" · checks {pi['checks']}" if pi.get("checks") else "") if pi else "PR " + url.rsplit("/", 1)[-1])
            ov = self.overlaps.by_id.get(s.get("id")) if b != "ended" else None
            if s.get("bg"): small.append("bg " + s["bg"])
            if cloud: small.append("cloud")
            elif (not d["chat"]) and str(s.get("cwd", "")).startswith("/"): small.append("wsl")
            if ov:
                warn = ("⚠ same folder as " if ov[0] == "tree" else "same branch as ") + ", ".join(ov[1])
                body.append(bg + (AMBER if ov[0] == "tree" else GREY) + "   " + fit(warn, cols - 4, "L") + RESET)
            if small: body.append(bg + GREY + "   " + fit("  ".join(small), cols - 4) + RESET)
            if s.get("id") in self.open:
                body.append("")
                body.extend(details(s, d, b, m, tasks))
            body.append("")
            if is_sel: sel_span = (start, len(body))

        for s, b in self.order:
            if b != "ended": add_session(s, b)
        if g["ended"]:
            body.append(GREY + f" {'▾' if self.show_ended else '▸'} {len(g['ended'])} exited or idle" + (" match" if query.strip() else "") + "  (e)" + RESET)
            if self.show_ended:
                body.append("")
                for s, b in self.order:
                    if b == "ended": add_session(s, b)
        if not self.order and not g["ended"]:
            body.append(GREY + (" Nothing matches." if query.strip() else " No Claude sessions yet. Start one and it shows up here.") + RESET)

        hints = (["Tab tasks"] if self.tasksv else []) + ["↑↓ select", "enter/space details"] + (["n new task"] if os.path.isfile(ct_command()) else []) + ["s new session",
                 "a go to/attach" if TABS else "a attach", "r reply", "x end"]
        tail = ["? help"] + ([f"u unhide {hid}"] if hid else [])
        for drop in ("x end", "r reply", "s new session", "n new task", "enter/space details", "a go to/attach", "a attach"):
            if width(" · ".join(hints + tail)) <= cols - 2: break
            hints.remove(drop)               # too wide: drop the least needed hints, so ? help never gets cut off
        foot_keys = " · ".join(hints + tail)
        return self._compose(head, body, sel_span, foot_keys, cols, rows, self)

    def _compose(self, head, body, sel_span, foot_keys, cols, rows, scroller):
        """footer (toast, key hints or the input box) and scrolling, shared by both views"""
        msg = self.toast if time.time() - self.toast_at < getattr(self, "toast_secs", 2) else ""
        if not msg and getattr(self, "pending", None): msg = "starting: " + self.pending + " …"
        foot = ["", " " + (GREEN + fit(msg, cols - 1) + RESET if msg else GREY + fit(foot_keys, cols - 1) + RESET)]
        if self.typing is not None:
            where = os.path.basename(self.typing_repo.rstrip("/")) or "?"
            label = self.input_label or ("reply to this session" if self.reply_to else f"new task in {where}")
            box = input_lines(self.typing + "▏", cols - 4)
            cap = max(3, rows // 3)                   # grows up to a third of the window, then scrolls
            if len(box) > cap: box = ["… " + str(len(box) - cap + 1) + " more line(s) above"] + box[-(cap - 1):]
            hint = "type to filter · ↑↓ move · enter keep · esc clear" if self.input_label == "search" else "enter send · esc cancel · paste works"
            foot = [GREY + f" {label}  ({hint})" + RESET] + \
                   [" " + (BOLD + "› " + RESET if i == 0 else "  ") + fit(line, cols - 4, "L") for i, line in enumerate(box)]

        avail = max(3, rows - len(head) - len(foot))
        a, z = sel_span
        top = scroller.top
        if a < top: top = a
        if z > top + avail: top = max(0, min(a, z - avail))
        top = max(0, min(top, max(0, len(body) - avail)))
        scroller.top = top
        view = body[top:top + avail]
        view += [""] * (avail - len(view))
        lines = head + view + foot
        if scroller is self:        # where the selected session's lines are on screen, for the attach hand-off
            n = 0
            while a + n < z and body[a + n]: n += 1
            self.shown = (lines[:rows], len(head) + a - top if top <= a and a + n <= top + avail else None, n)
        start = "\x1b[2J" if getattr(self, "clear", False) else ""   # window was resized: wipe what the old layout left behind
        self.clear = False
        return start + "\x1b[H" + "\n".join(l + RESET + "\x1b[K" for l in lines[:rows]) + "\x1b[J"

    def usage_line(self):
        r = (self.metrics.rate or {}).get("rate_limits") or {}
        out = []
        for k, label in (("five_hour", "5h"), ("seven_day", "7d")):
            w = r.get(k)
            if w and w.get("used_percentage") is not None and (w.get("resets_at") or 1e12) > time.time():
                out.append(f"{label} {w['used_percentage']:.0f}% (resets {time.strftime('%a %H:%M', time.localtime(w['resets_at']))})")
        if out: return "plan usage  " + "   ".join(out)
        w = self.metrics.window
        if w: return f"tokens used  5h {fmt_n(w['5h'])} · 7d {fmt_n(w['7d'])}  (plan % needs the statusline hook)"
        return "usage  counting…"

    def selected(self):
        return next(((s, b) for s, b in self.order if s.get("id") == self.sel), (None, None))

    def say(self, t, secs=2): self.toast, self.toast_at, self.toast_secs = t, time.time(), secs

    def ask(self, label, cb, initial="", allow_empty=False):
        """open the input box with a label; cb(text) runs on Enter"""
        self.typing, self.input_label, self.input_cb, self.input_empty = initial, label, cb, allow_empty

    def type_key(self, k):
        """keys while typing a new task (or a reply, or a search) in the footer"""
        if self.input_label == "search":       # live filter: the list narrows as you type, arrows move through it
            ids = [s.get("id") for s, _ in self.order]
            i = ids.index(self.sel) if self.sel in ids else -1
            if k in ("down", "up") and ids: self.sel = ids[min(len(ids) - 1, i + 1) if k == "down" else max(0, i - 1)]; return
            if k == "esc": self.search = ""
            if k in ("esc", "enter"): self.search = self.typing if k == "enter" else ""; self.typing = None; self.input_cb = None; self.input_label = None; self.top = 0; return
        if k == "esc": self.typing = None; self.reply_to = None; self.input_cb = None; self.input_label = None
        elif k == "enter" and self.input_cb:
            text, cb, empty = self.typing, self.input_cb, self.input_empty
            self.typing = None; self.input_cb = None; self.input_label = None
            if text.strip() or empty:
                try: cb(text)
                except Exception as e: self.say("Failed: " + str(e), 4)
        elif k == "enter":
            text, repo, to = self.typing.strip(), self.typing_repo, self.reply_to; self.typing = None; self.reply_to = None
            if text and to: self.reply(to, text, repo)
            elif text: self.launch(text, repo)
        elif k.startswith("\x00paste:"): self.typing += k[7:].replace("\r\n", "\n").replace("\r", "\n")
        elif k in ("\x7f", "\x08"): self.typing = self.typing[:-1]
        elif k and len(k) == 1 and k.isprintable(): self.typing += k

    def launch(self, task, repo):
        """start the task with the ct launcher in the background; the result shows as a toast"""
        self.pending = task
        def go():
            try:
                r = subprocess.run([ct_command(), "new", task, "--repo", repo, "--yes"], capture_output=True, text=True, timeout=300)
                lines = [l for l in (r.stdout + r.stderr).splitlines() if l.strip() and not l.startswith("routing")]
                self.say((lines[0] if lines else "ct finished") if r.returncode == 0 else "ct failed: " + (lines[-1] if lines else ""), 8)
            except Exception as e:
                self.say("ct failed: " + str(e), 8)
            self.pending = None
        threading.Thread(target=go, daemon=True).start()

    def reply(self, sid, text, cwd):
        """send a reply to a background session: it continues in the background with the reply as its next prompt"""
        self.say("Sending reply…")
        def go():
            try:
                r = subprocess.run(["claude", "--resume", sid, "--bg", text], capture_output=True, text=True, timeout=120, cwd=cwd or None)
                out = " ".join((r.stdout + r.stderr).split())
                self.say(("Reply sent" + (" - " + out[:80] if "note:" in out else "")) if r.returncode == 0 else "Reply failed: " + out[:100])
            except Exception as e:
                self.say("Reply failed: " + str(e))
        threading.Thread(target=go, daemon=True).start()

    def to_background(self, s, text):
        """an exited terminal session: continue the same conversation (same id) as a background session, so attach,
        reply and end work on it here. `claude agents` reports it within ~10 seconds."""
        self.say("Moving it to the background…")
        def go():
            try:
                r = subprocess.run(["claude", "--resume", s["id"], "--bg", text], capture_output=True, text=True, timeout=120, cwd=local_dir(s.get("cwd") or ""))
                out = " ".join((r.stdout + r.stderr).split())
                if r.returncode: self.say("Couldn't move it: " + out[:90], 6)
                elif "copy" in out: self.say("It was still running, so a copy started: " + out[:70], 8)
                else: self.say("In the background now - a attach · r reply", 6)
            except Exception as e:
                self.say("Couldn't move it: " + str(e), 6)
        threading.Thread(target=go, daemon=True).start()

    def end(self, s):
        """background session: stop it and hide its row. Its conversation and worktree stay (claude rm could delete a
        worktree that another session still uses).
        Other sessions run in their own terminal, the app or the cloud, so only their row is removed."""
        if s.get("bg"):
            def go():
                r = subprocess.run(["claude", "stop", s["bg"]], capture_output=True, text=True, timeout=60)
                out = " ".join((r.stdout + r.stderr).split())
                self.say("Session stopped (its work and worktree are kept)" if r.returncode == 0 else "Couldn't stop it: " + out[:90], 6)
            threading.Thread(target=go, daemon=True).start(); self.say("Ending session…")
        self.hidden["x:" + str(s.get("id"))] = s.get("lastEventAt") or 0; save_hidden(self.hidden)
        if not s.get("bg"):
            where = {"cloud": "end it in claude.ai", "chat": "it lives in the Desktop app"}.get(s.get("surface"), "close its terminal to end it")
            self.say("Row removed - " + where)

    def paint(self, screen, ms):
        """one frame of the attach motion: these lines from the top, the rest of the screen cleared"""
        rows = shutil.get_terminal_size((100, 30))[1]
        sys.stdout.write("\x1b[H" + "\n".join(l + RESET + "\x1b[K" for l in screen[:rows]) + "\x1b[J"); sys.stdout.flush()
        time.sleep(ms / 1000)

    def faded(self, lines, y0, n, c):
        """the screen with every line but the session's greyed to colour c"""
        return [l if y0 <= i < y0 + n else ESC + f"38;5;{c}m" + ANSI.sub("", l) for i, l in enumerate(lines)]

    def handoff(self, s, ready=None):
        """the rest of the board fades out and the selected row glides up to the top line, where it stays as the
        session's header over its last prompt and reply, with the way back at the bottom. With `ready`, the loader
        runs until claude attach (started before the motion) has drawn, instead of a fixed 0.9 s"""
        lines, y0, n = getattr(self, "shown", (None, None, 0))
        cols, rows = shutil.get_terminal_size((100, 30)); cols = max(30, cols - 1)
        show = self.paint
        row = lines[y0:y0 + n] if lines and y0 is not None and n else []
        if row:
            for c in (241, 238, 235): show(self.faded(lines, y0, n, c), 45)
            for k in range(7):
                show([""] * round(y0 * (1 - k / 6) ** 3) + row, 30)
        tl = [x for x in s.get("timeline") or [] if isinstance(x, dict) and x.get("k") in ("you", "say")]
        talk = []
        for k, label, most in (("you", "you", 2), ("say", "said", 4)):
            text = next((x.get("x") for x in reversed(tl) if x.get("k") == k), "")
            for j, line in enumerate(wrap(text, cols - 10, most) if text else []):
                talk.append("   " + GREY + (label if j == 0 else "").ljust(6) + RESET + fit(line, cols - 10, direction(text)))
        top = row + [""] if row else []
        talk = talk[:max(0, rows - 4 - len(top))]     # a short window keeps the loader and the way back on screen
        for i in range(1, len(talk) + 1): show(top + talk[:i], 45)
        foot = " " + GREEN + BOLD + "ctrl+z" + RESET + GREY + " back to the board" + RESET
        above = top + talk + ([""] if talk else [])
        t0, k = time.time(), 0
        while (not ready() and time.time() - t0 < 10) if ready else k < 3:     # while claude attach starts
            scr = above + ["   " + GREY + "opening the session" + " ·" * (k % 3 + 1) + RESET]
            show(scr + [""] * max(0, rows - 1 - len(scr)) + [foot], 0); k += 1
            for _ in range(6):
                if ready and ready(): break
                time.sleep(0.05)

    def handback(self):
        """back from an attached session: its row drops from the top line into its place and the board fades in
        around it. Quick (~0.15 s), because coming back is usually to do the next thing on the board."""
        self.shown = None
        final = self.frame()        # also finds where the session's row is now
        lines, y0, n = self.shown or (None, None, 0)
        if lines and y0 is not None and n:
            row = lines[y0:y0 + n]
            for k in range(5): self.paint([""] * round(y0 * (1 - (1 - k / 4) ** 3)) + row, 18)
            for c in (237, 241): self.paint(self.faded(lines, y0, n, c), 25)
        sys.stdout.write(final); sys.stdout.flush()

    def attach(self, s):
        """hand the terminal to `claude attach`; detaching (← on an empty prompt) comes back here"""
        cmd = ["claude", "attach", s["bg"]]
        child = None
        if os.name != "nt":
            try: child = Attached(cmd)       # starts now, so its start-up happens during the motion
            except Exception: child = None
        try: self.handoff(s, child.ready if child else None)
        except Exception: pass
        # stay on the board's own screen: printing on the shell's screen left one line there per attach, and they piled up
        sys.stdout.write("\x1b[?2004l\x1b[?25h\x1b[2J\x1b[H"); sys.stdout.flush()
        if self.term_old is not None: restore(self.term_old)
        try: child.run() if child else run_attached(cmd)
        except Exception as e: print("attach failed:", e); time.sleep(2)
        restore(self.term_old)      # claude may have left the terminal raw
        self.term_old = raw_mode()
        sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[?2004h\x1b[2J"); sys.stdout.flush(); self.clear = True
        try: self.handback()
        except Exception: pass

    def jump(self, s):
        """a session running in its own terminal: switch Windows Terminal to its tab. A WSL session's terminal gets a
        one-off title for a moment, so the tab is found exactly even when two tabs look alike; then its title goes back."""
        if not TABS: self.say("It runs in another terminal window - switch to it yourself (jumping to tabs needs Windows Terminal)", 5); return
        title = s.get("name") or s.get("aiTitle") or ""
        term = str(s.get("term") or "").split(":")
        tty = session_tty(term[2]) if len(term) == 3 and term[0] == "wsl" and term[1] == os.environ.get("WSL_DISTRO_NAME") else None
        if term[0] == "wsl" and not tty and term[1:2] == [os.environ.get("WSL_DISTRO_NAME")]:
            self.say("Its terminal is gone - the session may have exited", 4); return
        if not tty and not title: self.say("Can't tell which tab it is (no title yet)", 4); return
        self.say("Finding its tab…")
        def go():
            stop = threading.Event()
            try:
                p = subprocess.Popen(focus_cmd("tab"), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
                p.stdout.readline()                    # the tabs as they were; the script answers with the found one's old title
                if tty:
                    marker = "csb-" + secrets.token_hex(4)
                    def keep():                        # Claude Code redraws its title (the spinner) while working: keep the marker up
                        while not stop.is_set(): set_title(tty, marker); stop.wait(0.05)
                    threading.Thread(target=keep, daemon=True).start()
                    p.stdin.write("find " + marker + "\n")
                else:
                    p.stdin.write("match " + title.replace("\n", " ") + "\n")
                p.stdin.flush()
                out, _ = p.communicate(timeout=20)
                stop.set(); time.sleep(0.06)
                r = json.loads((out.strip().splitlines() or ["{}"])[-1])
                if tty: set_title(tty, r.get("was") or title)
                self.say("Switched to its tab" if r.get("ok") else (r.get("why") or "Couldn't find its tab"), 2 if r.get("ok") else 5)
            except Exception as e:
                stop.set()
                try: p.kill()
                except Exception: pass
                if tty: set_title(tty, title)
                self.say("Couldn't switch: " + str(e)[:90], 5)
        threading.Thread(target=go, daemon=True).start()

    def focus_app(self, s):
        """a Desktop chat: bring the Claude app to the front. The app shows no chat list to other programs and the
        board's chat ids aren't claude.ai ids, so the chat's title goes to the clipboard to find it there."""
        if not TABS: self.say("Bringing the Claude app forward needs Windows - open it yourself", 5); return
        title = s.get("name") or s.get("aiTitle") or ""
        def go():
            try:
                r = subprocess.run(focus_cmd("app"), capture_output=True, text=True, encoding="utf-8", timeout=20)
                d = json.loads((r.stdout.strip().splitlines() or ["{}"])[-1])
                if d.get("ok"): self.say("Claude app in front · " + (copy(title, "chat title") if title else "pick the chat"), 4)
                else: self.say(d.get("why") or "Couldn't bring the Claude app up", 5)
            except Exception as e:
                self.say("Couldn't bring the Claude app up: " + str(e)[:80], 5)
        threading.Thread(target=go, daemon=True).start()

    def picker_key(self, k):
        p = self.picker
        if k in ("esc", "q", "Q", "\x03"): self.picker = None; self.clear = True
        elif k in ("down", "j"): p.sel = min(len(p.items) - 1, p.sel + 1)
        elif k in ("up", "k"): p.sel = max(0, p.sel - 1)
        elif k in ("right", "l") and p.items[p.sel][0] != "here": p.go(p.items[p.sel][1])
        elif k in ("left", "h", "\x7f", "\x08"): p.up()
        elif k in ("/", "~"):
            def to(text):
                d = os.path.abspath(os.path.expanduser(local_dir(text.strip()) or text.strip()))
                if os.path.isdir(d): p.go(d)
                else: self.say("Not a folder: " + text.strip(), 3)
            self.ask("go to folder", to, initial=("~" if k == "~" else p.path.rstrip("/") + "/"))
        elif k == "enter": self.new_session(p.items[p.sel][1])

    def new_session(self, path, args=""):
        """open a new Windows Terminal tab running claude (plus `args`, e.g. --resume <id>) in `path`; Windows Terminal
        switches to the new tab itself. After claude exits the tab stays open on a shell in that folder."""
        self.picker = None; self.clear = True
        if not TABS:       # no tab to open here: hand over the command instead
            self.say(copy(("cd " + shlex.quote(path) + " && claude " + args).strip(), "Command to start it") + " - paste it in a terminal", 6); return
        if os.name == "nt":
            cmd = ["wt", "-w", "0", "new-tab", "-d", path, "cmd", "/k", ("claude " + args).strip()]
        else:
            wt = shutil.which("wt.exe")
            if not wt: self.say("Windows Terminal (wt.exe) isn't reachable from here", 4); return
            distro = os.environ.get("WSL_DISTRO_NAME")
            # no ";" anywhere: wt reads it as "next command"
            cmd = [wt, "-w", "0", "new-tab", "wsl.exe"] + (["-d", distro] if distro else []) + ["--cd", path, "--", "bash", "-lic", ("claude " + args).strip() + " || true && exec bash"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            out = " ".join((r.stdout + r.stderr).split())
            self.say((f"Resumed in {base(path)} - in the new tab" if args else f"Started claude in {base(path)} - in the new tab") if r.returncode == 0 else "Couldn't open a tab: " + out[:90], 4)
        except Exception as e:
            self.say("Couldn't open a tab: " + str(e)[:90], 4)

    def key(self, k):
        if self.typing is not None: self.type_key(k); return True
        if self.picker: self.picker_key(k); return True
        if self.help:
            if k in ("down", "j") and self.help.scrolls: self.help.top += 1
            elif k in ("up", "k") and self.help.scrolls: self.help.top = max(0, self.help.top - 1)
            else: self.help = None; self.clear = True
            return True
        if k == "\t" and self.tasksv:
            self.view = "tasks" if self.view == "sessions" else "sessions"; self.clear = True; return True
        if self.view == "tasks" and self.tasksv:
            if k in ("q", "Q", "\x03"): return False
            self.tasksv.key(k); return True
        ids = [s.get("id") for s, _ in self.order]
        i = ids.index(self.sel) if self.sel in ids else -1
        if k in ("q", "Q", "\x03"): return False
        if k in ("down", "j") and ids: self.sel = ids[min(len(ids) - 1, i + 1)]
        elif k in ("up", "k") and ids: self.sel = ids[max(0, i - 1)]
        elif k in ("enter", " ") and self.sel:
            self.open.symmetric_difference_update({self.sel})
        elif k == "e": self.show_ended = not self.show_ended
        elif k == "?": self.help = Help(); self.clear = True
        elif k == "/": self.ask("search", lambda t: None, initial=self.search, allow_empty=True)
        elif k == "n" and not os.path.isfile(ct_command()):
            self.say("n starts background tasks with the ct launcher - turn it on with: csb-tui.py --setup", 6)
        elif k == "n":
            s, _ = self.selected()
            cwd = (s or {}).get("cwd") or ""
            local = local_dir(cwd)
            if not local or not os.path.isdir(local) or (s or {}).get("surface") in ("chat", "cloud"):
                self.say("Select a local session in the repo first (new tasks start in the selected session's folder)")
            else:
                self.typing, self.typing_repo = "", local
        elif k == "s":
            loader = getattr(self, "loader", None)
            self.picker = Picker(recent_folders(loader.sessions if loader else load_sessions()), start_folder())
        elif k == "u": self.hidden = {}; save_hidden(self.hidden); self.say("Showing hidden sessions again")
        else:
            s, b = self.selected()
            if not s: return True
            if k == "a":
                if s.get("bg"): self.attach(s)
                elif s.get("surface") == "chat": self.focus_app(s)
                elif s.get("surface") == "cloud":
                    self.say(copy(s["url"], "Session link") if s.get("url") else "Cloud sessions open in claude.ai")
                elif s.get("state") == "ended": self.say("It has exited: b continues it in the background · c resumes it in a new tab", 4)
                else: self.jump(s)
            elif k == "r" and s.get("bg") and (self.agents.by_session.get(s.get("id")) or {}).get("pid"):
                # the session's process is still running: a reply from outside would start a copy, so open it to type there
                self.attach(s)
            elif k == "x":
                if self.confirm_x == s.get("id") and time.time() - self.confirm_at < 3:
                    self.confirm_x = None; self.end(s)
                else:
                    self.confirm_x, self.confirm_at = s.get("id"), time.time()
                    self.say("Press x again to end this session" if s.get("bg") else "Press x again to remove this row (the session itself runs elsewhere)")
            elif k == "r":
                if s.get("bg"):
                    self.typing, self.reply_to = "", s.get("id")
                    cwd = s.get("cwd") or ""
                    self.typing_repo = cwd if cwd.startswith("/") else ""
                else: self.say("Replies go to background sessions; this one runs in its own terminal")
            elif k == "b":
                if s.get("bg"): self.say("Already in the background: a attach · r reply")
                elif s.get("surface") in ("chat", "cloud"): self.say("Only terminal sessions can move to the background")
                elif s.get("state") != "ended": self.say("Exit it in its terminal first (/exit), then press b - otherwise it starts a copy", 4)
                elif not os.path.isdir(local_dir(s.get("cwd") or "")): self.say("Its folder isn't reachable from here")
                else: self.ask("first prompt for it in the background", lambda text, s=s: self.to_background(s, text), initial="continue")
            elif k == "c":
                if s.get("surface") == "chat": self.say("Desktop chats can't be resumed from a terminal")
                elif s.get("surface") == "cloud":
                    self.say(copy(s["url"], "Session link") if s.get("url") else copy("claude --teleport", "Teleport command (pick the session)"))
                elif s.get("bg"): self.attach(s)
                elif s.get("state") != "ended": self.jump(s)     # still running: a resume would start a copy
                elif not os.path.isdir(local_dir(s.get("cwd") or "")): self.say("Its folder isn't reachable from here")
                else: self.new_session(local_dir(s.get("cwd")), "--resume " + shlex.quote(str(s.get("id"))))
            elif k == "f":
                if s.get("surface") not in ("chat", "cloud"): self.say(copy(s.get("cwd", ""), "Folder path"))
            elif k == "h":
                if b in ("turn", "ended"):
                    self.hidden[s.get("id")] = s.get("t") or 0; save_hidden(self.hidden); self.say("Hidden until it changes")
                else: self.say("Only finished or exited sessions can be hidden")
        return True

def local_dir(cwd):
    """a session folder as this Python sees it: Windows paths (C:\\...) become /mnt/c/... under WSL"""
    if cwd.startswith("/") or os.name == "nt": return cwd
    return re.sub(r"^([A-Za-z]):[\\/]", lambda m: "/mnt/" + m.group(1).lower() + "/", cwd).replace("\\", "/") if re.match(r"^[A-Za-z]:", cwd) else ""

def home_short(p):
    h = os.path.expanduser("~")
    return "~" + p[len(h):] if p == h or p.startswith(h + os.sep) else p

def recent_folders(sessions, n=6):
    """folders of recent local sessions, newest first; worktrees are left out (they belong to one task)"""
    out = []
    for s in sorted(sessions, key=lambda s: s.get("lastEventAt") or 0, reverse=True):
        if s.get("surface") in ("chat", "cloud"): continue
        d = local_dir(str(s.get("cwd") or "")).rstrip("\\/")
        if d and d not in out and "/.claude/worktrees/" not in d.replace("\\", "/") + "/" and os.path.isdir(d): out.append(d)
        if len(out) == n: break
    return out

def start_folder():
    h = os.path.expanduser("~")
    return os.path.join(h, "projects") if os.path.isdir(os.path.join(h, "projects")) else h

class Picker:
    """the folder browser behind `s`: recent session folders, then `path` itself and the folders inside it"""
    def __init__(self, recent, start):
        self.recent = recent; self.top = 0
        self.go(start); self.sel = 0
    def go(self, path, select=None):
        self.path = os.path.abspath(path)
        try:
            with os.scandir(self.path) as it:
                names = sorted((e.name for e in it if not e.name.startswith(".") and e.is_dir()), key=str.lower)
        except OSError:
            names = []
        self.items = [("recent", r) for r in self.recent] + [("here", self.path)] + [("dir", os.path.join(self.path, x)) for x in names]
        self.sel = next((i for i, it in enumerate(self.items) if it[0] == "dir" and it[1] == select), len(self.recent))
    def up(self):
        parent = os.path.dirname(self.path)
        if parent != self.path: self.go(parent, select=self.path)
    def render(self, cols):
        head = [f" {BOLD}new session{RESET}{GREY}   pick the folder claude starts in{RESET}", ""]
        body, span = [], (0, 0)
        for i, (kind, p) in enumerate(self.items):
            if kind == "recent" and i == 0: body.append(GREY + " recent" + RESET)
            if kind == "here": body += ["", GREY + " " + fit("── " + home_short(self.path) + " ──", cols - 2) + RESET]
            text = home_short(p) if kind == "recent" else ". (start here)" if kind == "here" else os.path.basename(p) + "/"
            on = i == self.sel
            if on: span = (len(body), len(body) + 1)
            body.append((SEL if on else "") + ("▸ " if on else "  ") + ("" if kind == "dir" else GREY if kind == "here" and not on else "") + fit(text, cols - 4) + RESET)
        if len(self.items) == len(self.recent) + 1: body.append(GREY + "   no folders inside" + RESET)
        keys = "↑↓ move · → open · ← up · enter start claude here · / type a path · esc cancel"
        return head, body, span, keys

class Help:
    """the full key list behind `?`: two columns when the window is wide enough, keys in blue"""
    LEFT = [
        ("Move", [("↑↓", "select a session"), ("enter", "open or close its details"), ("Tab", "tasks view"),
                  ("/", "search, exited ones too"), ("e", "show exited and idle"), ("esc", "clear search, close rows")]),
        ("Start", [("n", "new task in this folder"), ("s", "new session in a folder" if TABS else "folder + command to start claude there")]),
        ("Board", [("u", "show hidden again"), ("?", "this help"), ("q", "quit")]),
    ]
    RIGHT = [
        ("Selected session", [("a", "go to it, or attach" if TABS else "attach a background session"), ("", "Ctrl+Z comes back"), ("r", "reply (background only)"),
                              ("b", "continue exited in background"), ("c", "resume in a new tab" if TABS else "copy the resume command"),
                              ("x x", "end it or remove the row"), ("f", "copy folder path"), ("h", "hide until it changes")]),
    ]

    def __init__(self): self.top = 0; self.scrolls = False

    @staticmethod
    def column(groups):
        out = []
        for title, keys in groups:
            out.append(BOLD + title + RESET)
            for k, what in keys: out.append(BLUE + k.ljust(7) + RESET + what if k else " " * 7 + GREY + what + RESET)
            out.append("")
        return out

    def render(self, cols, rows):
        head = [" " + BOLD + "help" + RESET + GREY + "   every key on the sessions view" + RESET, ""]
        left, right = self.column(self.LEFT), self.column(self.RIGHT)
        if cols >= 84:
            body = ["  " + l + " " * (44 - width(re.sub(r"\x1b\[[0-9;]*m", "", l))) + r
                    for l, r in itertools.zip_longest(left, right, fillvalue="")]
        else: body = ["  " + l for l in left + right]
        avail = rows - len(head) - 2
        self.scrolls = len(body) > avail
        self.top = min(self.top, max(0, len(body) - avail))
        return head, body, (self.top, self.top), "↑↓ scroll · any other key closes" if self.scrolls else "any key closes"


def session_tty(pid):
    """the terminal a WSL Claude process draws on (/dev/pts/N), when it is still that same Claude process"""
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as fh:
            if b"claude" not in fh.read(): return None
        for fd in (0, 1, 2):
            t = os.readlink(f"/proc/{int(pid)}/fd/{fd}")
            if t.startswith("/dev/pts/"): return t
    except (OSError, ValueError):
        pass
    return None

def set_title(tty, title):
    """set a terminal's window/tab title (OSC 0) by writing to its tty; only changes the title, never the screen"""
    try:
        with open(tty, "w", encoding="utf-8") as fh: fh.write("\x1b]0;" + re.sub(r"[\x00-\x1f\x7f]", " ", title) + "\x07")
    except OSError:
        pass

def focus_cmd(mode):
    """powershell command line for csb-focus.ps1 (next to this file)"""
    ps1 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "csb-focus.ps1")
    if os.name != "nt":
        ps1 = subprocess.run(["wslpath", "-w", ps1], capture_output=True, text=True).stdout.strip() or ps1
    return ["powershell" if os.name == "nt" else "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", ps1, "-Mode", mode]

def copy(text, label):
    for cmd in (["clip.exe"], ["clip"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["pbcopy"]):
        if shutil.which(cmd[0]):
            try:
                enc = "utf-16-le" if cmd[0].startswith("clip") else "utf-8"
                subprocess.run(cmd, input=text.encode(enc), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return f"{label} copied"
            except Exception:
                pass
    return f"{label}: {text}"

# ---------- keyboard ----------
if os.name == "nt":
    import msvcrt
    def read_key(timeout):
        end = time.time() + timeout
        while time.time() < end:
            if msvcrt.kbhit():
                c = msvcrt.getwch()
                if c in ("\x00", "\xe0"):
                    return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(msvcrt.getwch(), "")
                if c == "\r": return "enter"
                if c == "\x1b": return "esc"
                return c
            time.sleep(0.03)
        return None
    def raw_mode(): os.system(""); return None      # os.system("") turns on ANSI colours in the Windows console
    def restore(_): pass
else:
    import select, termios, tty
    def read_key(timeout):
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if not r: return None
        b = os.read(sys.stdin.fileno(), 1)
        if b and b[0] >= 0xC0:      # first byte of a multi-byte UTF-8 character: read the rest
            b += os.read(sys.stdin.fileno(), 3 if b[0] >= 0xF0 else 2 if b[0] >= 0xE0 else 1)
        c = b.decode(errors="ignore")
        if c == "\x1b":
            r, _, _ = select.select([sys.stdin], [], [], 0.03)
            if r:
                seq = os.read(sys.stdin.fileno(), 8).decode(errors="ignore")
                if seq.startswith("[200~"):           # bracketed paste: read until the end marker
                    buf = seq[5:].encode()
                    while b"\x1b[201~" not in buf:
                        r, _, _ = select.select([sys.stdin], [], [], 0.5)
                        if not r: break
                        buf += os.read(sys.stdin.fileno(), 4096)
                    return "\x00paste:" + buf.split(b"\x1b[201~")[0].decode(errors="ignore")
                return {"[A": "up", "[B": "down", "OA": "up", "OB": "down", "[C": "right", "[D": "left", "OC": "right", "OD": "left"}.get(seq[:2], "")
            return "esc"
        if c in ("\r", "\n"): return "enter"
        return c
    def raw_mode():
        old = termios.tcgetattr(sys.stdin); tty.setcbreak(sys.stdin.fileno()); return old
    def restore(old): termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)

def run_attached(cmd):
    """run `claude attach` in this terminal. Its Ctrl+Z stops the whole foreground job, which used to suspend the board
    too and drop to the shell; the board ignores it, and a stopped attach is ended (the session keeps running)."""
    if os.name == "nt": subprocess.run(cmd); return
    import signal
    prev = signal.signal(signal.SIGTSTP, signal.SIG_IGN)
    try:
        p = subprocess.Popen(cmd, preexec_fn=lambda: signal.signal(signal.SIGTSTP, signal.SIG_DFL))
        _, st = os.waitpid(p.pid, os.WUNTRACED)
        if os.WIFSTOPPED(st):
            p.terminate(); os.kill(p.pid, signal.SIGCONT)
            try: p.wait(3)
            except subprocess.TimeoutExpired: p.kill(); p.wait()
    finally:
        signal.signal(signal.SIGTSTP, prev)

class Attached:
    """`claude attach` in a pty the board owns, started when the hand-off motion starts. Its start-up (0.5-1 s) used to
    be a black screen after the motion; now it happens behind it. Its output is held until the motion ends, then shown,
    and keys and window size pass through until it exits. Ctrl+Z is caught here: in a pty of its own it can't stop the job."""
    CTRL_Z = re.compile(rb"\x1a|\x1b\[122;5(?::1)?u")      # plain, and as the kitty keyboard protocol claude turns on sends it
    def __init__(self, cmd):
        import pty
        self.buf, self.at, self.hold, self.dead = [], 0, True, False
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try: os.execvp(cmd[0], cmd)
            finally: os._exit(127)
        self.size()
        self.t = threading.Thread(target=self.collect, daemon=True); self.t.start()
    def size(self):
        import fcntl
        try: fcntl.ioctl(self.fd, termios.TIOCSWINSZ, fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b"\0" * 8))
        except OSError: pass
    def read(self):
        try: return os.read(self.fd, 65536)
        except OSError: return b""
    def collect(self):
        while self.hold:
            if not select.select([self.fd], [], [], 0.05)[0]: continue
            b = self.read()
            if not b: self.dead = True; return
            self.buf.append(b); self.at = time.time()
    def ready(self):
        """its first screen is up: more than its one "Attaching…" line came and then went quiet, or it already exited"""
        return self.dead or sum(map(len, self.buf)) > 400 and time.time() - self.at > 0.08
    def run(self):
        import signal
        self.hold = False; self.t.join()
        out = sys.stdout.buffer; out.write(b"".join(self.buf)); out.flush()
        old = termios.tcgetattr(0); tty.setraw(0)
        resized = [False]
        prev = signal.signal(signal.SIGWINCH, lambda *a: resized.__setitem__(0, True)), signal.signal(signal.SIGTSTP, signal.SIG_IGN)
        try:
            while not self.dead:
                if resized[0]: resized[0] = False; self.size()
                r = select.select([0, self.fd], [], [], 0.2)[0]
                if self.fd in r:
                    b = self.read()
                    if not b: break
                    out.write(b); out.flush()
                if 0 in r:
                    b = os.read(0, 4096)
                    if self.CTRL_Z.search(b): break
                    os.write(self.fd, b)
                pid, st = os.waitpid(self.pid, os.WNOHANG | os.WUNTRACED)
                if pid: break        # exited, or stopped itself
        finally:
            termios.tcsetattr(0, termios.TCSADRAIN, old)
            signal.signal(signal.SIGWINCH, prev[0]); signal.signal(signal.SIGTSTP, prev[1])
            self.close()
    def close(self):
        """end the attach client (the session keeps running) and undo the terminal modes it may have left on"""
        import signal
        for sig in (signal.SIGTERM, signal.SIGCONT):
            try: os.kill(self.pid, sig)
            except OSError: pass
        end = time.time() + 3
        while time.time() < end:
            try:
                if os.waitpid(self.pid, os.WNOHANG)[0]: break
            except ChildProcessError: break
            time.sleep(0.02)
        else:
            os.kill(self.pid, signal.SIGKILL); os.waitpid(self.pid, 0)
        os.close(self.fd)
        sys.stdout.write("\x1b[<u\x1b[>4;0m\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1004l\x1b[?2031l\x1b[?2026l"); sys.stdout.flush()

def run_setup(force):
    """the first-run questions (csb_wizard.py): what to switch on, asked before anything optional is touched"""
    import csb_wizard
    old = raw_mode(); sys.stdout.write("\x1b[?1049h\x1b[?25l"); sys.stdout.flush()
    try: csb_wizard.run(read_key, force)
    finally:
        sys.stdout.write("\x1b[?25h\x1b[?1049l"); sys.stdout.flush(); restore(old)

def main():
    if "--test" in sys.argv:
        toast("Claude Sessions", "Test notification - it works.")
        cfg = ntfy_config()
        if not cfg.get("topic") or cfg.get("enabled") is False: print("Phone pushes are off. Turn them on with: csb-tui.py --setup"); return
        push(cfg, "Claude Sessions", "Test push - it works.", False)
        print("Subscribe to this topic in the ntfy app:", cfg.get("topic"), "on", cfg.get("server", "https://ntfy.sh")); time.sleep(3); return
    if "--uninstall" in sys.argv:
        import csb_setup
        print("\n".join(csb_setup.uninstall())); return
    if sys.stdin.isatty() and ("--setup" in sys.argv or __import__("csb_setup").needs_setup()):
        run_setup("--setup" in sys.argv)
    try: os.makedirs(SESS, exist_ok=True)          # a fresh install has no sessions until the first hook runs
    except OSError:
        print(f"Couldn't create the session board folder at {BASE}. Set CSB_DIR to a folder you can write to."); return
    try:
        import csb_prune
        threading.Thread(target=csb_prune.prune, args=(BASE,), daemon=True).start()
    except Exception:
        pass
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass
    global CLOUD
    CLOUD = CloudFeed()
    old = raw_mode(); app = App(); app.term_old = old; app.loader = Loader(app, Notifier())
    sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b[?2004h"); sys.stdout.flush()   # own screen, hide cursor
    try:
        last = 0
        while True:
            if time.time() - last >= TICK:
                sys.stdout.write(app.frame()); sys.stdout.flush(); last = time.time()
            k = read_key(max(0.05, TICK - (time.time() - last)))
            if k is None: continue
            if k == "esc" and app.typing is None and app.picker is None and app.help is None and app.view == "sessions":
                if app.search: app.search = ""; app.top = 0     # esc first clears a search, then closes open rows
                else: app.open.clear()
            elif not app.key(k): break
            sys.stdout.write(app.frame()); sys.stdout.flush(); last = time.time()
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\x1b[?2004l\x1b[?25h\x1b[?1049l"); sys.stdout.flush(); restore(old)

if __name__ == "__main__":
    main()
