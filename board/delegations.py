"""Delegated and reviewable work: session-board/delegations.json (board only, never Todoist).

Every task you give Claude from a Todoist task (`c`), or start with `ct --review`, is one item (plain ct / n launches
are ordinary sessions and aren't recorded):
  id (session UUID), bg (short id), title, repo, kind (code|work), tier, todoist_id, result_dir, task,
  review: what you'll test yourself ("" = no review)
  state: with_claude -> done                 (no review: the task just shows "done by Claude")
         with_claude -> for_review -> approved | rejected   (review asked for; send back returns it to with_claude)
  summary, pr, created, updated
`ct` adds items; the board moves them along as their sessions finish. See docs/spec-delegation.md."""
import json, os, threading, time


class Delegations:
    def __init__(self, base):
        self.file = os.path.join(base, "delegations.json")
        self.lock = threading.Lock()
        self.data, self.mtime = {"items": {}}, 0

    def _load(self):
        try:
            m = os.path.getmtime(self.file)
            if m != self.mtime:
                with open(self.file, encoding="utf-8") as fh: self.data = json.load(fh)
                self.mtime = m
        except FileNotFoundError:
            pass
        except Exception:
            pass        # half-written by ct: keep the last good copy

    def items(self):
        with self.lock:
            self._load()
            return [dict(v) for v in self.data.get("items", {}).values()]

    def update(self, item_id, **fields):
        with self.lock:
            self.mtime = 0; self._load()              # always merge into the newest copy on disk
            it = self.data.setdefault("items", {}).get(item_id)
            if it is None: return
            it.update(fields); it["updated"] = int(time.time() * 1000)
            tmp = self.file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh: json.dump(self.data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.file); self.mtime = os.path.getmtime(self.file)

    def sync(self, sessions, prs_of):
        """move items along from their sessions' state: a finished turn means done, or ready to test if you asked to"""
        by_id = {s.get("id"): s for s in sessions}
        for it in self.items():
            s = by_id.get(it["id"])
            if not s: continue
            pr = (prs_of(it["id"]) or [None])[-1]
            if it["state"] == "with_claude" and s.get("state") in ("yourturn", "error"):
                self.update(it["id"], state="for_review" if it.get("review") else "done", summary=s.get("lastReply") or s.get("detail") or "", pr=pr or it.get("pr"),
                            failed=s.get("state") == "error")
            elif it["state"] in ("for_review", "done") and s.get("state") in ("working", "needs"):
                self.update(it["id"], state="with_claude")      # you replied from the session itself
            elif pr and pr != it.get("pr"):
                self.update(it["id"], pr=pr)
