"""Your own tasks (Todoist) as a second view of the board. Tab switches between sessions and tasks.

Tabs: Today (overdue + today) · Upcoming (next 7 days) · Projects (all projects, sections and task trees) · Search.
Claude tab (5): work you gave Claude, and the review queue - see docs/spec-delegation.md.
Keys: ↑↓ select · ←→ or 1-5 tab · c give to Claude · a add · s add subtask · m move (make it a subtask / move to a project) · e edit
      d due date · p priority · space complete · u undo complete · D delete (twice) · enter fold · / search · r refresh"""
import datetime, json, os, re, shutil, subprocess, sys, threading, time

from todoist import Todoist
from delegations import Delegations

WORK = os.path.expanduser("~/claude-work")

PRIO_COL = {4: "\x1b[38;5;203m", 3: "\x1b[38;5;208m", 2: "\x1b[38;5;111m", 1: "\x1b[38;5;245m"}   # p1 red ... p4 grey
TABS = ["today", "upcoming", "projects", "search", "claude"]
TAB_NAME = {"today": "Today", "upcoming": "Upcoming", "projects": "Projects", "search": "Search", "claude": "Claude"}


def due_day(t):
    d = (t.get("due") or {}).get("date") or ""
    try: return datetime.date.fromisoformat(d[:10])
    except ValueError: return None


def due_label(t, today):
    due = t.get("due") or {}
    day = due_day(t)
    if not day: return "", ""
    time_part = due["date"][11:16] if len(due.get("date", "")) > 10 else ""
    delta = (day - today).days
    if delta < 0: txt, kind = (f"{-delta}d overdue" if delta < -1 else "yesterday"), "over"
    elif delta == 0: txt, kind = "today", "today"
    elif delta == 1: txt, kind = "tomorrow", "soon"
    elif delta < 7: txt, kind = day.strftime("%a"), "soon"
    else: txt, kind = day.strftime("%-d %b"), ""
    if time_part: txt += " " + time_part
    if due.get("is_recurring"): txt = "↻ " + txt
    return txt, kind


class TasksView:
    def __init__(self, app, ui):
        self.app, self.ui = app, ui              # ui = the dashboard module (colours and text helpers)
        self.td = Todoist(ui.BASE)
        self.dl = Delegations(ui.BASE)
        self.last_repo = ""
        threading.Thread(target=self._sync_loop, daemon=True).start()
        self.tab, self.query = "today", ""
        self.sel, self.top = None, 0             # selected row key: ("task"|"project"|"section", id)
        self.order = []                          # selectable row keys in display order
        self.moving = None                       # task id being moved
        self.del_confirm, self.del_at = None, 0
        self.opened = set(); self.deleg_by_task = {}
        self.ui_file = os.path.join(ui.BASE, "tasks-ui.json")
        try:
            with open(self.ui_file, encoding="utf-8") as fh: self.folded = set(json.load(fh).get("folded", []))
        except Exception:
            self.folded = None                   # first run: fold every project except the Inbox once projects load

    def _sync_loop(self):
        while True:
            try:
                loader = getattr(self.app, "loader", None)
                if loader: self.dl.sync(loader.sessions, lambda sid: (self.app.m.get(sid) or {}).get("prs"))
            except Exception:
                pass
            time.sleep(2)

    # ---------- helpers ----------
    def save_ui(self):
        try:
            with open(self.ui_file, "w", encoding="utf-8") as fh: json.dump({"folded": sorted(self.folded or [])}, fh)
        except Exception:
            pass

    def bg(self, label, fn, *a, done=None):
        """run a Todoist call off the key path; the result shows as a toast"""
        self.app.say(label + "…", 30)
        def go():
            r = fn(*a)
            self.app.say((done or "Done") if r is not None and r is not False else (self.td.error or "Failed"), 4)
        threading.Thread(target=go, daemon=True).start()

    def selected_task(self):
        if self.sel and self.sel[0] == "task": return self.td.tasks.get(self.sel[1])
        return None

    def context(self):
        """(project_id, section_id) of where the selection is, for adding tasks there"""
        k = self.sel
        if not k: return None, None
        if k[0] == "project": return k[1], None
        if k[0] == "section":
            s = self.td.sections.get(k[1]) or {}
            return s.get("project_id"), k[1]
        t = self.td.tasks.get(k[1]) or {}
        return t.get("project_id"), t.get("section_id")

    def descendants(self, tid, tasks):
        out, stack = set(), [tid]
        while stack:
            cur = stack.pop()
            for t in tasks.values():
                if t.get("parent_id") == cur and t["id"] not in out: out.add(t["id"]); stack.append(t["id"])
        return out

    # ---------- rows ----------
    def task_rows(self, roots, tasks, depth, today, show_project, projects):
        """task trees: each root followed by its subtasks (unless folded)"""
        rows = []
        kids = {}
        for t in tasks.values():
            if t.get("parent_id"): kids.setdefault(t["parent_id"], []).append(t)
        def walk(t, d):
            n = len(kids.get(t["id"], []))
            rows.append({"kind": "task", "id": t["id"], "depth": d, "task": t, "kids": n, "show_project": show_project})
            if n and ("task", t["id"]) not in (self.folded or set()):
                for c in sorted(kids[t["id"]], key=lambda x: x.get("child_order", 0)): walk(c, d + 1)
        for t in roots: walk(t, depth)
        return rows

    def build(self, today):
        projects, sections, tasks = self.td.snapshot()
        if self.folded is None and projects:
            self.folded = {("project", p) for p, v in projects.items() if not v.get("inbox_project")}
            self.folded = set(self.folded)
        folded = self.folded or set()
        rows = []
        if self.tab in ("today", "upcoming", "search"):
            if self.tab == "search":
                q = self.query.lower()
                pick = [t for t in tasks.values() if q and (q in t.get("content", "").lower() or q in t.get("description", "").lower())]
                groups = [("", pick)] if q else []
            else:
                horizon = today if self.tab == "today" else today + datetime.timedelta(days=7)
                pick = [t for t in tasks.values() if due_day(t) and due_day(t) <= horizon]
                byday = {}
                for t in pick:
                    d = due_day(t)
                    key = "Overdue" if d < today else ("Today" if d == today else ("Tomorrow" if (d - today).days == 1 else d.strftime("%A %-d %b")))
                    byday.setdefault((min(d, today) if d < today else d, key), []).append(t)
                groups = [(k[1], v) for k, v in sorted(byday.items())]
            ids = {t["id"] for t in pick}
            if self.tab == "today":
                tests = [i for i in self.dl.items() if i["state"] == "for_review"]
                if tests:
                    rows.append({"kind": "date", "text": "To test", "n": len(tests)})
                    rows += [{"kind": "review", "id": i["id"], "item": i} for i in sorted(tests, key=lambda x: x.get("updated", 0))]
            for label, items in groups:
                if label: rows.append({"kind": "date", "text": label, "n": len(items)})
                inset = {t["id"] for t in items}
                # nest a task under its parent when the parent is listed in the same group
                roots = [t for t in items if t.get("parent_id") not in inset]
                roots.sort(key=lambda t: (-t.get("priority", 1), (t.get("due") or {}).get("date", ""), t.get("child_order", 0)))
                sub = {k: v for k, v in tasks.items() if k in inset}
                rows += self.task_rows(roots, sub, 0, today, True, projects)
            if self.tab == "search" and not self.query:
                rows.append({"kind": "info", "text": "Press / and type to search all your tasks."})
            elif not pick:
                rows.append({"kind": "info", "text": "Nothing here." if self.td.synced_at else "Loading your tasks…"})
        else:
            pkids = {}
            for p in projects.values(): pkids.setdefault(p.get("parent_id"), []).append(p)
            def proj(p, depth):
                ptasks = {k: v for k, v in tasks.items() if v.get("project_id") == p["id"]}
                rows.append({"kind": "project", "id": p["id"], "depth": depth, "text": p.get("name", "?"), "n": len(ptasks)})
                if ("project", p["id"]) not in folded:
                    loose = [t for t in ptasks.values() if not t.get("parent_id") and not t.get("section_id")]
                    rows.extend(self.task_rows(sorted(loose, key=lambda t: t.get("child_order", 0)), ptasks, depth + 1, today, False, projects))
                    for s in sorted([s for s in sections.values() if s.get("project_id") == p["id"]], key=lambda s: s.get("section_order", 0)):
                        stasks = [t for t in ptasks.values() if t.get("section_id") == s["id"] and not t.get("parent_id")]
                        rows.append({"kind": "section", "id": s["id"], "depth": depth + 1, "text": s.get("name", "?"), "n": len(stasks)})
                        if ("section", s["id"]) not in folded:
                            rows.extend(self.task_rows(sorted(stasks, key=lambda t: t.get("child_order", 0)), ptasks, depth + 2, today, False, projects))
                for c in sorted(pkids.get(p["id"], []), key=lambda x: x.get("child_order", 0)): proj(c, depth + 1)
            roots = sorted(pkids.get(None, []), key=lambda p: (not p.get("inbox_project"), p.get("child_order", 0)))
            for p in roots: proj(p, 0)
            if not projects: rows.append({"kind": "info", "text": "Loading your projects…"})
        return rows, projects

    # ---------- render ----------
    def render(self, cols, rows_avail):
        ui = self.ui; G, R, B = ui.GREY, ui.RESET, ui.BOLD
        today = datetime.date.today()
        tabline = " " + B + "tasks" + R + G + "  ·  sessions (Tab)" + R + "   " + "  ".join(
            (B + "▸" + TAB_NAME[t] + R) if t == self.tab else (G + str(i + 1) + " " + TAB_NAME[t] + R) for i, t in enumerate(TABS))
        if self.tab == "search" and self.query: tabline += G + f'   "{self.query}"' + R
        status = ("syncing…" if self.td.busy else "synced " + time.strftime("%H:%M", time.localtime(self.td.synced_at)) if self.td.synced_at else "")
        head = [tabline, G + " " + ui.clip(self.td.error or status or " ", cols - 2) + R, ""]
        if not self.td.token and self.tab != "claude":
            body = ["", " Connect your Todoist account:", "",
                    "  1. In Todoist open Settings → Integrations → Developer and copy your API token",
                    "  2. Press t here and paste it, then Enter", "",
                    G + "  The token is saved only in the session-board folder (todoist.json), never in a repo." + R]
            return head, body, (0, 0), "t paste token · Tab sessions · q quit"
        self.deleg_by_task = {it["todoist_id"]: it["state"] for it in sorted(self.dl.items(), key=lambda x: x.get("created", 0))
                              if it.get("todoist_id") and it["state"] in ("with_claude", "for_review", "done")}
        if self.tab == "claude": return self.render_claude(head, cols)
        rows, projects = self.build(today)
        body, span, order = [], (0, 0), []
        moving_off = self.descendants(self.moving, self.td.tasks) | {self.moving} if self.moving else set()
        for r in rows:
            k = (r["kind"], r.get("id"))
            if r["kind"] in ("task", "project", "section", "review"): order.append(k)
            if k == self.sel and self.sel is not None and r["kind"] != "info": start = len(body)
            sel = k == self.sel
            bg = ui.SEL if sel else ""
            mark = "▸" if sel else " "
            if r["kind"] == "date":
                body.append(""); body.append(" " + (ui.RED if r["text"] == "Overdue" else B) + r["text"] + R + G + f"  {r['n']}" + R)
            elif r["kind"] == "info":
                body.append(G + "  " + r["text"] + R)
            elif r["kind"] == "review":
                it = r["item"]
                body.append(bg + mark + ui.AMBER + "□ " + R + bg + B + "Test: " + R + bg + ui.fit(it.get("title", ""), cols - 30) + R + bg
                            + ("  " + ui.RED + "failed" + R + bg if it.get("failed") else "") + R)
                body.append(bg + G + "    check: " + ui.fit(it.get("review", ""), cols - 12) + R)
                where = it.get("pr") or it.get("result_dir") or ""
                if sel: body.append(bg + G + "    " + ui.fit((("Claude: " + " ".join(str(it.get("summary", "")).split())) if it.get("summary") else ""), cols - 6) + R)
                if where: body.append(bg + G + "    " + ui.fit(where + "   (o open · A approve · b send back · X reject)", cols - 6) + R)
            elif r["kind"] in ("project", "section"):
                fold = "▸" if k in (self.folded or set()) else "▾"
                ind = "  " * r["depth"]
                name = ("# " if r["kind"] == "project" else "") + r["text"]
                # indentation stays outside fit(), which collapses leading spaces
                body.append(bg + mark + ind + (B if r["kind"] == "project" else ui.BLUE) + (fold + " " + ui.fit(name, cols - 11 - len(ind)))
                            + R + bg + G + str(r["n"]).rjust(6) + "  " + R)
            else:
                t = r["task"]; ind = "  " * r["depth"]
                circ = PRIO_COL.get(t.get("priority", 1), "") + ("◉" if self.moving == t["id"] else "○") + R + bg
                dtxt, dkind = due_label(t, today)
                dcol = ui.RED if dkind == "over" else ui.GREEN if dkind == "today" else ui.AMBER if dkind == "soon" else G
                extra = []
                if r["kids"]: extra.append(f"↳{r['kids']}" + (" ▸" if ("task", t["id"]) in (self.folded or set()) else ""))
                if t.get("description"): extra.append("≡")
                dstate = self.deleg_by_task.get(t["id"])
                if dstate == "with_claude": extra.append("→ Claude")
                elif dstate == "for_review": extra.append("to test")
                elif dstate == "done": extra.append("done by Claude")
                proj = "#" + (projects.get(t.get("project_id")) or {}).get("name", "") if r["show_project"] else ""
                right_plain = "  ".join(x for x in [" ".join(extra), dtxt, proj] if x)
                width_left = max(10, cols - 6 - len(ind) - ui.width(right_plain) - 3)
                dim = G if t["id"] in moving_off and self.moving else ""
                left = bg + mark + ind + circ + " " + dim + ui.fit(t.get("content", ""), width_left) + R + bg
                right = (G + " ".join(extra) + R + bg + ("  " if extra and (dtxt or proj) else "")) + (dcol + dtxt + R + bg if dtxt else "") + \
                        (("  " if dtxt else "") + G + ui.fit(proj, min(24, ui.width(proj))) + R + bg if proj else "")
                body.append(left + "  " + right + " " + R)
            if sel and r["kind"] != "info": span = (start, len(body))
        self.order = order
        if self.sel not in order: self.sel = order[0] if order else None
        if self.moving:
            keys = "moving: select the new parent task, a project or a section · enter place it here · esc cancel"
        else:
            keys = "↑↓ · ←→ tabs · a add · s subtask · space done · u undo · e edit · d due · p priority · m move · D delete · / search · Tab sessions"
        return head, body, span, keys

    # ---------- keys ----------
    def key(self, k):
        app = self.app
        if k == "t" and not self.td.token:
            app.ask("paste your Todoist API token", lambda v: v and (self.td.set_token(v), app.say("Token saved - loading your tasks…", 4))); return
        if k in ("down", "j", "up", "k") and self.order:
            i = self.order.index(self.sel) if self.sel in self.order else 0
            self.sel = self.order[min(len(self.order) - 1, i + 1) if k in ("down", "j") else max(0, i - 1)]; return
        if not self.td.token and self.tab != "claude" and k not in ("right", "left", "5"): return
        if k in ("right", "left"):
            i = TABS.index(self.tab); self.tab = TABS[(i + (1 if k == "right" else -1)) % len(TABS)]; self.sel = None; return
        if k in ("1", "2", "3", "4", "5"): self.tab = TABS[int(k) - 1]; self.sel = None; return
        if self.tab == "claude": self.claude_key(k); return
        if k == "/":
            self.tab = "search"
            app.ask("search tasks", lambda v: setattr(self, "query", v.strip()), initial=self.query); return
        if k == "r": self.td.resync_soon(); app.say("Syncing with Todoist…"); return
        if k == "esc":
            if self.moving: self.moving = None; app.say("Move cancelled")
            return
        if self.sel and self.sel[0] == "review" and k in ("enter", "o", "a", "b", "X", "A"):
            self.claude_key(k); return
        t = self.selected_task()
        if k == "enter":
            if self.moving: self.place(); return
            if self.sel:
                f = self.folded if self.folded is not None else set()
                f.symmetric_difference_update({self.sel}); self.folded = f; self.save_ui()
            return
        if k == "a":
            pid, sid = self.context() if self.tab == "projects" else (None, None)
            where = ("#" + self.td.projects.get(pid, {}).get("name", "")) if pid else ("today" if self.tab == "today" else "Inbox")
            def add(v):
                if not v.strip(): return
                def go():
                    nt = self.td.quick_add(v.strip())
                    if not nt: return None
                    if pid and "#" not in v:
                        self.td.move(nt["id"], section_id=sid) if sid else self.td.move(nt["id"], project_id=pid)
                    if self.tab == "today" and not nt.get("due"): self.td.update(nt["id"], due_string="today")
                    return nt
                self.bg("Adding", go, done="Added")
            app.ask(f"new task in {where}  (dates, #project, p1-p4 and @labels work)", add); return
        if not t:
            if k in ("s", "space", " ", "e", "d", "p", "m", "D"): app.say("Select a task first")
            return
        if k == "s":
            app.ask(f"new subtask of: {t['content'][:50]}", lambda v: v.strip() and self.bg("Adding subtask", self.td.quick_add, v.strip(), t["id"], done="Subtask added")); return
        if k in (" ", "space"):
            self.bg("Completing", self.td.close, t["id"], done=f"Done: {t['content'][:40]} · u to undo"); return
        if k == "u":
            if self.td.done: last = self.td.done[-1]; self.bg("Reopening", self.td.reopen, last["id"], done="Reopened: " + last["content"][:40])
            else: app.say("Nothing to undo")
            return
        if k == "e":
            app.ask("edit task", lambda v: v.strip() and self.bg("Saving", self.td.update, t["id"], content=v.strip(), done="Saved"), initial=t.get("content", "")); return
        if k == "d":
            app.ask("due date (e.g. tomorrow 9am, every monday, 25 oct; empty = no date)",
                    lambda v: self.bg("Setting date", self.td.update, t["id"], due_string=v.strip() or "no date", done="Date set"),
                    initial=(t.get("due") or {}).get("string", ""), allow_empty=True); return
        if k == "p":
            nxt = {1: 4, 4: 3, 3: 2, 2: 1}[t.get("priority", 1)]
            self.bg("Setting priority", self.td.update, t["id"], priority=nxt, done=f"Priority p{5 - nxt}"); return
        if k == "m":
            self.moving = t["id"]; app.say("Pick where it goes, then Enter", 4); return
        if k == "c":
            self.delegate(t); return
        if k == "D":
            if self.del_confirm == t["id"] and time.time() - self.del_at < 3:
                self.del_confirm = None; self.bg("Deleting", self.td.delete, t["id"], done="Deleted")
            else:
                self.del_confirm, self.del_at = t["id"], time.time(); app.say("Press D again to delete: " + t["content"][:50], 3)
            return

    def place(self):
        tid, self.moving = self.moving, None
        k = self.sel
        if not k: return
        if k[0] == "task":
            if k[1] == tid or k[1] in self.descendants(tid, self.td.tasks):
                self.app.say("A task can't go under itself or its own subtask"); return
            self.bg("Moving", self.td.move, tid, k[1], done="Now a subtask of: " + self.td.tasks.get(k[1], {}).get("content", "")[:40])
        elif k[0] == "section":
            self.bg("Moving", self.td.move, tid, None, None, k[1], done="Moved to section")
        else:
            self.bg("Moving", self.td.move, tid, None, k[1], done="Moved to #" + self.td.projects.get(k[1], {}).get("name", ""))


# ---------- Claude tab: delegated work and the review queue ----------

def _age(ms):
    s = max(0, int(time.time() - ms / 1000))
    return f"{s // 60}m" if s < 3600 else f"{s // 3600}h" if s < 86400 else f"{s // 86400}d"


def _open(target):
    """open a URL or folder in the desktop's browser / file manager: Explorer on Windows and WSL, Finder on macOS, xdg-open elsewhere"""
    try:
        quiet = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.name == "nt":
            if target.startswith("http"): subprocess.Popen(["cmd", "/c", "start", "", target], **quiet)
            else: subprocess.Popen(["explorer", target], **quiet)
        elif shutil.which("wslpath"):                 # WSL
            if target.startswith("http"): subprocess.Popen(["cmd.exe", "/c", "start", "", target], cwd="/mnt/c", **quiet)
            else:
                w = subprocess.run(["wslpath", "-w", target], capture_output=True, text=True).stdout.strip() or target
                subprocess.Popen(["explorer.exe", w], cwd="/mnt/c", **quiet)
        else:
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            if not shutil.which(opener): return False
            subprocess.Popen([opener, target], **quiet)
        return True
    except Exception:
        return False


def _render_claude(self, head, cols):
    ui = self.ui; G, R, B = ui.GREY, ui.RESET, ui.BOLD
    items = self.dl.items()
    sessions = {s.get("id"): s for s in (getattr(getattr(self.app, "loader", None), "sessions", None) or [])}
    recent = time.time() * 1000 - 7 * 86400000
    groups = [("To test", [i for i in items if i["state"] == "for_review"]), ("With Claude", [i for i in items if i["state"] == "with_claude"]),
              ("Done by Claude", [i for i in items if i["state"] == "done" and (i.get("updated") or 0) > recent])]
    body, order, span = [], [], (0, 0)
    for label, its in groups:
        if not its and label == "Done by Claude": continue
        body.append(""); body.append(" " + (ui.AMBER if label == "To test" else ui.GREEN if label == "Done by Claude" else ui.BLUE) + B + label + R + G + f"  {len(its)}" + R)
        if not its: body.append(G + ("   Nothing for you to test." if label == "To test" else "   Nothing delegated right now.") + R)
        for it in sorted(its, key=lambda x: -(x.get("updated") or x.get("created") or 0)):
            k = ("deleg", it["id"]); order.append(k); sel = k == self.sel; bg = ui.SEL if sel else ""
            start = len(body)
            s = sessions.get(it["id"]) or {}
            if it["state"] == "with_claude":
                st = {"needs": (ui.AMBER, "needs you"), "working": (ui.BLUE, "working")}.get(s.get("state"), (G, "queued"))
            else:
                st = (ui.RED, "failed") if it.get("failed") else (ui.AMBER, "to test") if it["state"] == "for_review" else (ui.GREEN, "done")
            where = "work" if it.get("kind") == "work" else os.path.basename(it.get("repo", "").rstrip("/"))
            right = f"{st[1]} · {_age(it.get('updated') or it.get('created') or 0)}"
            todo = "  ✓ todoist" if it.get("todoist_id") else ""
            body.append(bg + ("▸" if sel else " ") + st[0] + "● " + R + bg + B + ui.fit(it.get("title", "?"), cols - 30 - len(where)) + R + bg
                        + G + "  " + where + todo + R + bg + "  " + st[0] + right + R)
            line2 = it.get("summary") if it["state"] in ("for_review", "done") else (s.get("detail") or "")
            if line2: body.append(bg + G + "   " + ui.fit(" ".join(str(line2).split()), cols - 4) + R)
            pr = it.get("pr")
            if pr:
                pi = self.app.prs.get(pr) if getattr(self.app, "prs", None) else None
                body.append(bg + G + "   PR #" + pr.rsplit("/", 1)[-1] + (f" {pi['state']}" + (f" · checks {pi['checks']}" if pi.get("checks") else "") if pi else "") + R)
            elif it.get("result_dir"): body.append(bg + G + "   result: " + it["result_dir"] + R)
            if sel and ("open", it["id"]) in self.opened:
                ev = self.packet_evidence(it)
                for line in ev: body.append(bg + G + "   " + ui.fit(line, cols - 4) + R)
                body.append(bg + G + "   task: " + ui.fit(" ".join(it.get("task", "").split()), cols - 10) + R)
            body.append("")
            if sel: span = (start, len(body))
    self.order = order
    if self.sel not in order: self.sel = order[0] if order else None
    keys = "↑↓ · ←→ tabs · enter details · a open session · o open PR/result · to test: A approve · b send back · X reject · Tab sessions"
    return head, body, span, keys


def _packet_evidence(self, it):
    """reviewer verdict and evidence lines from the task's packet, if the session wrote one"""
    out = []
    try:
        import glob
        for f in glob.glob(os.path.join(it.get("repo", ""), "tasks", "*.md")) + glob.glob(os.path.join(it.get("repo", ""), ".claude", "worktrees", "*", "tasks", "*.md")):
            txt = open(f, encoding="utf-8").read(4000)
            if it.get("bg") and it["bg"] not in txt and it.get("title", "") not in txt: continue
            for key in ("reviewer", "evidence"):
                m = re.search(rf"^{key}:\s*(.+)$", txt, re.M | re.I)
                if m: out.append(f"{key}: {m.group(1).strip()}")
            if out: break
    except Exception:
        pass
    return out or ["no reviewer verdict recorded"]


def _claude_key(self, k):
    app = self.app
    if k in ("down", "j", "up", "k") and self.order:
        i = self.order.index(self.sel) if self.sel in self.order else 0
        self.sel = self.order[min(len(self.order) - 1, i + 1) if k in ("down", "j") else max(0, i - 1)]; return
    if not self.sel: return
    it = next((x for x in self.dl.items() if x["id"] == self.sel[1]), None)
    if not it: return
    if k == "enter":
        self.opened.symmetric_difference_update({("open", it["id"])}); return
    if k == "o":
        target = it.get("pr") or it.get("result_dir")
        app.say(("Opening " + target) if target and _open(target) else "Nothing to open yet", 3); return
    if k == "a":
        if it.get("bg"): app.attach(it["bg"])
        else: app.say("No background session to open")
        return
    if k == "b":
        def back(note):
            live = (app.agents.by_session.get(it["id"]) or {}).get("pid")
            self.dl.update(it["id"], state="with_claude")
            if live: app.say("It's still running - type your note there; Ctrl+Z comes back", 4); app.attach(it["bg"])
            else: app.reply(it["id"], note, it.get("repo"))
        app.ask("send back with a note (what to change)", back); return
    if k == "X":
        if self.del_confirm == it["id"] and time.time() - self.del_at < 3:
            self.del_confirm = None
            if it.get("bg"): subprocess.Popen(["claude", "stop", it["bg"]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.dl.update(it["id"], state="rejected"); app.say("Rejected - the session is stopped and the task is yours again", 4)
        else:
            self.del_confirm, self.del_at = it["id"], time.time(); app.say("Press X again to reject: " + it.get("title", "")[:50], 3)
        return
    if k == "A":
        if it["state"] != "for_review": app.say("Only tasks you asked to test are approved here; check the Todoist task off when you're happy"); return
        def approve():
            pr = it.get("pr")
            if pr and it.get("kind") != "work":
                r = subprocess.run(["gh", "pr", "view", pr, "--json", "state,statusCheckRollup,mergeable"], capture_output=True, text=True, timeout=60)
                try: d = json.loads(r.stdout)
                except Exception: return "Couldn't read the PR: " + (r.stderr or "")[:80]
                if d.get("state") == "OPEN":
                    checks = [str(c.get("conclusion") or c.get("state") or c.get("status")).upper() for c in d.get("statusCheckRollup") or []]
                    if any(c in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT") for c in checks): return "Not merged: checks are failing. Send it back or fix it first."
                    if any(c in ("PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED") for c in checks): return "Not merged yet: checks are still running. Try again when they finish."
                    m = subprocess.run(["gh", "pr", "merge", pr, "--squash", "--delete-branch"], capture_output=True, text=True, timeout=120)
                    if m.returncode != 0: return "Merge failed: " + " ".join((m.stderr or m.stdout).split())[:100]
            if it.get("todoist_id"): self.td.close(it["todoist_id"])
            self.dl.update(it["id"], state="approved")
            return "Approved" + (" and merged" if pr and it.get("kind") != "work" else "") + (" · Todoist task completed" if it.get("todoist_id") else "")
        app.say("Approving…", 30)
        threading.Thread(target=lambda: app.say(approve(), 6), daemon=True).start(); return


def _delegate(self, t):
    """give a Todoist task to Claude: where it runs, definition of done, extra context, then start it with ct"""
    app = self.app
    ans = {}
    def step3(ctx):
        ans["ctx"] = ctx.strip()
        app.say("Starting: " + t["content"][:50] + " …", 60)
        threading.Thread(target=self._launch, args=(t, ans), daemon=True).start()
    def step_review(rv):
        ans["review"] = rv.strip()
        app.ask("extra context: file paths, links, an earlier session or PR, notes (Enter to skip)", step3, allow_empty=True)
    def step2(dod):
        ans["dod"] = dod.strip()
        app.ask("will you test it yourself? Enter = no, or type what you'll check", step_review, allow_empty=True)
    def step1(where):
        where = where.strip()
        ans["where"] = "work" if where in ("", "work", "w") else os.path.expanduser(where)
        if ans["where"] != "work": self.last_repo = where
        app.ask("definition of done: what proves it's finished?", step2)
    app.ask("give to Claude - where: a repo path for code, or Enter for non-code work (research, writing, admin)", step1,
            initial=self.last_repo, allow_empty=True)


def _launch(self, t, ans):
    app = self.app
    notes = (t.get("description") or "").strip()
    comments = [c for c in self.td.comments(t["id"]) if c.strip()]
    work = ans["where"] == "work"
    slug = time.strftime("%Y-%m-%d-") + re.sub(r"[^\w֐-׿]+", "-", t["content"].lower()).strip("-")[:40]
    result_dir = os.path.join(WORK, slug) if work else None
    parts = [f"Task (from my Todoist): {t['content']}"]
    if notes: parts.append("Notes:\n" + notes)
    if comments: parts.append("Comments:\n" + "\n".join("- " + c for c in comments))
    if ans.get("ctx"): parts.append("Context:\n" + ans["ctx"])
    parts.append("Definition of done:\n" + ans["dod"])
    if work:
        parts.append(f"This is not a code task. Put the result in {result_dir}/ (create it; Markdown unless the task needs another "
                     "format) and commit it. Don't send emails or messages, post, buy, or change anything outside that folder "
                     "without asking me first.")
    parts.append("Use the task-intake skill. When you finish, end with a short summary of the result.")
    cmd = [shutil.which("ct") or os.path.expanduser("~/.local/bin/ct"), "new", "\n\n".join(parts), "--json", "--todoist-id", t["id"]]
    if ans.get("review"): cmd += ["--review", ans["review"]]
    cmd += ["--kind", "work"] + (["--result-dir", result_dir] if result_dir else []) if work else ["--repo", ans["where"]]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        last = (r.stdout.strip().splitlines() or [""])[-1]
        d = json.loads(last) if last.startswith("{") else {}
        if d.get("ok"): app.say(f"With Claude: {d['name']} ({d['tier']}, {d['model']})" + (" - you'll get a Test item in Today when it's done" if ans.get("review") else ""), 8)
        else: app.say("Couldn't start it: " + " ".join((r.stdout + r.stderr).split())[-120:], 10)
    except Exception as e:
        app.say("Couldn't start it: " + str(e), 8)


TasksView.render_claude = _render_claude
TasksView.packet_evidence = _packet_evidence
TasksView.claude_key = _claude_key
TasksView.delegate = _delegate
TasksView._launch = _launch
