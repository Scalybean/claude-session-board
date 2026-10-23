"""Todoist client for the task board (Todoist API v1, https://developer.todoist.com/api/v1/).

Todoist stays the source of truth: everything here reads from or writes to Todoist, and a background thread re-syncs
every minute so changes made in the Todoist apps show up too. The API token lives in <session-board>/todoist.json
({"token": "..."}) or the TODOIST_TOKEN environment variable - never in the repo.

Priority follows the API: 4 = p1 (urgent, red) ... 1 = p4 (normal)."""
import json, os, threading, time, urllib.error, urllib.parse, urllib.request, uuid

API = "https://api.todoist.com/api/v1"
RESYNC = 60          # seconds between background syncs


class TodoistError(Exception):
    pass


class Todoist:
    def __init__(self, base_dir):
        self.cfg_file = os.path.join(base_dir, "todoist.json")
        self.token = os.environ.get("TODOIST_TOKEN") or self._read_token()
        self.projects, self.sections, self.tasks = {}, {}, {}
        self.done = []             # tasks completed from the board this run, newest last (for undo)
        self.error, self.synced_at, self.busy = "", 0, False
        self.lock = threading.Lock()
        self._wake = threading.Event()
        if self.token: threading.Thread(target=self._loop, daemon=True).start()

    # ---------- token ----------
    def _read_token(self):
        try:
            with open(self.cfg_file, encoding="utf-8") as fh: return json.load(fh).get("token", "")
        except Exception:
            return ""

    def set_token(self, token):
        self.token = token.strip()
        tmp = self.cfg_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh: json.dump({"token": self.token}, fh)
        os.replace(tmp, self.cfg_file)
        threading.Thread(target=self._loop, daemon=True).start()

    # ---------- http ----------
    def _req(self, method, path, params=None, body=None):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": "Bearer " + self.token}
        if data is not None:
            headers["Content-Type"] = "application/json"; headers["X-Request-Id"] = str(uuid.uuid4())
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="ignore")[:200]
            raise TodoistError({401: "Todoist rejected the API token", 403: "Todoist refused access",
                                429: "Todoist rate limit, retrying shortly"}.get(e.code, f"Todoist error {e.code}: {msg}"))
        except urllib.error.URLError as e:
            raise TodoistError(f"Can't reach Todoist ({e.reason})")

    def _all(self, path, params=None):
        out, cursor = [], None
        while True:
            p = dict(params or {}, limit=200)
            if cursor: p["cursor"] = cursor
            d = self._req("GET", path, p)
            out += d.get("results", []) if isinstance(d, dict) else d
            cursor = d.get("next_cursor") if isinstance(d, dict) else None
            if not cursor: return out

    # ---------- sync ----------
    def sync(self):
        projects = {p["id"]: p for p in self._all("/projects")}
        sections = {s["id"]: s for s in self._all("/sections")}
        tasks = {t["id"]: t for t in self._all("/tasks")}
        with self.lock:
            self.projects, self.sections, self.tasks = projects, sections, tasks
            self.synced_at, self.error = time.time(), ""

    def _loop(self):
        while self.token:
            try: self.busy = True; self.sync()
            except TodoistError as e: self.error = str(e)
            except Exception as e: self.error = f"Sync failed: {e}"
            finally: self.busy = False
            self._wake.wait(RESYNC); self._wake.clear()

    def resync_soon(self):
        self._wake.set()

    # ---------- writes (each updates the local copy at once, then asks for a resync) ----------
    def _run(self, fn, *a, **kw):
        try:
            r = fn(*a, **kw); self.error = ""; return r
        except TodoistError as e:
            self.error = str(e); return None
        finally:
            self.resync_soon()

    def quick_add(self, text, parent_id=None):
        """Todoist's own quick-add parsing: "Pay rent tomorrow 9am #Home p1 @errand". Subtasks use the plain endpoint
        with the date part still understood through due_string."""
        def go():
            if parent_id:
                t = self._req("POST", "/tasks", body={"content": text, "parent_id": parent_id})
            else:
                t = self._req("POST", "/tasks/quick", body={"text": text})
            if t and t.get("id"):
                with self.lock: self.tasks[t["id"]] = t
            return t
        return self._run(go)

    def add(self, content, **fields):
        def go():
            t = self._req("POST", "/tasks", body=dict({"content": content}, **{k: v for k, v in fields.items() if v is not None}))
            if t and t.get("id"):
                with self.lock: self.tasks[t["id"]] = t
            return t
        return self._run(go)

    def update(self, task_id, **fields):
        def go():
            t = self._req("POST", f"/tasks/{task_id}", body=fields)
            if t and t.get("id"):
                with self.lock: self.tasks[t["id"]] = t
            return t
        return self._run(go)

    def close(self, task_id):
        def go():
            self._req("POST", f"/tasks/{task_id}/close")
            with self.lock:
                t = self.tasks.pop(task_id, None)
                kids = [k for k, v in self.tasks.items() if v.get("parent_id") == task_id]
                for k in kids: self.tasks.pop(k, None)      # Todoist completes subtasks with their parent
            if t: self.done.append(t)
            return True
        return self._run(go)

    def reopen(self, task_id):
        def go():
            self._req("POST", f"/tasks/{task_id}/reopen")
            with self.lock:
                for i, t in enumerate(self.done):
                    if t["id"] == task_id: self.tasks[task_id] = self.done.pop(i); break
            return True
        return self._run(go)

    def delete(self, task_id):
        def go():
            self._req("DELETE", f"/tasks/{task_id}")
            with self.lock: self.tasks.pop(task_id, None)
            return True
        return self._run(go)

    def move(self, task_id, parent_id=None, project_id=None, section_id=None):
        """exactly one target: under a task, to a project's top level, or into a section"""
        body = {"parent_id": parent_id} if parent_id else {"section_id": section_id} if section_id else {"project_id": project_id}
        def go():
            t = self._req("POST", f"/tasks/{task_id}/move", body=body)
            if t and t.get("id"):
                with self.lock: self.tasks[t["id"]] = t
            return t
        return self._run(go)

    def comments(self, task_id):
        try: return [c.get("content", "") for c in self._all("/comments", {"task_id": task_id})]
        except Exception: return []

    # ---------- reads for the view ----------
    def snapshot(self):
        with self.lock:
            return dict(self.projects), dict(self.sections), dict(self.tasks)
