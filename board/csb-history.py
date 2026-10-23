#!/usr/bin/env python3
"""Session board - history. Async hook (UserPromptSubmit + Stop) that keeps a compact log of every session outside the
repos: history/<YYYY-MM>.jsonl next to this file. Replaces the old "append every prompt to docs/activity_logs.md" rule,
at zero model tokens and without merge conflicts. One line per event: time, session, folder, prompt or reply."""
import glob, json, os, sys, time

from csb_paths import find_dir

def main():
    ev = json.load(sys.stdin)
    if ev.get("agent_id"): return
    name = ev.get("hook_event_name")
    text = ev.get("prompt") if name == "UserPromptSubmit" else ev.get("last_assistant_message") if name == "Stop" else None
    if not text or str(text).lstrip().startswith("<"): return       # skip injected system messages
    row = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": ev.get("session_id"), "cwd": ev.get("cwd"),
           "who": "you" if name == "UserPromptSubmit" else "claude", "text": " ".join(str(text).split())[:2000]}
    d = os.path.join(find_dir(), "history"); os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, time.strftime("%Y-%m") + ".jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")

if __name__ == "__main__":
    try: main()
    except Exception: pass
