#!/usr/bin/env python3
"""Session board - cloud reporter. Runs as a hook inside Claude Code cloud sessions and posts the session's state,
title and steps to a private ntfy topic that the local dashboard reads. Installed by the environment setup script."""
import json, os, re, sys, time, urllib.request

TOPIC = "__BOARD_TOPIC__"
SERVER = "https://ntfy.sh"
STATE_DIR = "/tmp/board-report"

def clip(s, n): s = " ".join(str(s or "").split()); return s if len(s) <= n else s[:n - 1] + "…"

def read_transcript(path):
    """steps and title from the session transcript"""
    tasks, title = [], ""
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if '"aiTitle"' in line:
                    m = re.search(r'"aiTitle":\s*"((?:[^"\\]|\\.)*)"', line)
                    if m: title = json.loads('"' + m.group(1) + '"')
                if '"TaskCreate"' not in line and '"TaskUpdate"' not in line and '"TodoWrite"' not in line: continue
                try: d = json.loads(line)
                except Exception: continue
                for b in ((d.get("message") or {}).get("content") or []) if isinstance(d.get("message"), dict) else []:
                    if not isinstance(b, dict) or b.get("type") != "tool_use": continue
                    n, i = b.get("name"), b.get("input") or {}
                    if n == "TodoWrite": tasks = [{"s": t.get("content", ""), "st": t.get("status", "pending")} for t in i.get("todos") or []]
                    elif n == "TaskCreate": tasks.append({"s": i.get("subject", ""), "st": "pending"})
                    elif n == "TaskUpdate":
                        k = int(str(i.get("taskId", "0")) or 0) - 1
                        if 0 <= k < len(tasks):
                            if i.get("status") == "deleted": tasks.pop(k)
                            elif i.get("status"): tasks[k]["st"] = i["status"]
    except Exception:
        pass
    return [{"s": clip(t["s"], 80), "st": t["st"]} for t in tasks[:25]], title

def main():
    if os.environ.get("CLAUDE_CODE_REMOTE") != "true": return      # only report from cloud sessions
    ev = json.load(sys.stdin)
    if ev.get("agent_id") or not ev.get("session_id"): return
    sid, name = ev["session_id"], ev.get("hook_event_name")
    state = {"SessionStart": "ready", "UserPromptSubmit": "working", "PostToolUse": "working", "Stop": "yourturn",
             "StopFailure": "error", "SessionEnd": "ended"}.get(name)
    if name == "Notification" and ev.get("notification_type") in ("permission_prompt", "elicitation_dialog"): state = "needs"
    if not state: return
    os.makedirs(STATE_DIR, exist_ok=True); last_f = os.path.join(STATE_DIR, sid)
    try: last = json.load(open(last_f))
    except Exception: last = {"t": 0, "state": ""}
    if name == "PostToolUse" and last["state"] == "working" and time.time() - last["t"] < 20: return   # throttle while working
    tasks, title = read_transcript(ev.get("transcript_path") or "")
    detail = {"needs": clip(ev.get("message"), 160) or "Waiting for your answer", "error": clip("Stopped with an error " + str(ev.get("error") or ""), 160),
              "yourturn": "Finished, your turn", "working": clip(ev.get("prompt"), 160) if name == "UserPromptSubmit" else "", "ended": "Session closed", "ready": "Started"}[state]
    p = {"v": 1, "id": sid, "state": state, "detail": detail, "title": clip(title, 80), "repo": os.path.basename((ev.get("cwd") or "").rstrip("/")),
         "tasks": tasks, "ts": int(time.time() * 1000)}
    rid = os.environ.get("CLAUDE_CODE_REMOTE_SESSION_ID", "")
    if rid: p["url"] = "https://claude.ai/code/" + (rid.replace("cse_", "session_", 1) if rid.startswith("cse_") else rid)
    if name == "Stop": p["reply"] = clip(ev.get("last_assistant_message"), 220)
    if name == "UserPromptSubmit" and not str(ev.get("prompt") or "").lstrip().startswith("<"): p["prompt"] = clip(ev.get("prompt"), 160)
    if str(p["detail"]).lstrip('"').startswith("<"): p["detail"] = ""      # injected system messages aren't your requests
    body = json.dumps(p, ensure_ascii=False)
    while len(body.encode()) > 3800 and p["tasks"]: p["tasks"].pop(); body = json.dumps(p, ensure_ascii=False)
    req = urllib.request.Request(SERVER, data=json.dumps({"topic": TOPIC, "message": body}).encode(), headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()
    json.dump({"t": time.time(), "state": state}, open(last_f, "w"))

if __name__ == "__main__":
    try: main()
    except Exception: pass      # never get in the session's way
