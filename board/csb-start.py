#!/usr/bin/env python3
"""Session board - start hook. Synchronous SessionStart + UserPromptSubmit hook.
1. Records where the session starts in its repo (csb_session.record), so the Stop guards judge only this session's work.
2. Tells the session, once per other session, when another live Claude session works in the same git working tree
   (or on the same branch of the same repo), so it doesn't revert or commit work that isn't its own.
Silent otherwise; never blocks."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import csb_session as cs

def main():
    ev = json.load(sys.stdin)
    if ev.get("agent_id") or not ev.get("session_id"): return      # subagents share the parent's tree on purpose
    sid, name, cwd = str(ev["session_id"]), ev.get("hook_event_name"), ev.get("cwd") or ""
    if name == "SessionStart" and ev.get("source") == "compact": return
    cs.budget(7)                     # the hook has 10 s; git on the Windows drive can be slow
    cs.record(sid, cwd)
    me = cs.tree_info(cwd)
    if not me: return
    d = cs.load(sid); warned = set(d.get("warned") or [])
    found = []
    for s in cs.live_sessions():
        if s.get("id") == sid or s.get("id") in warned: continue
        kind = cs.overlap(me, cs.tree_info(s.get("cwd")))
        if kind: found.append((kind, s))
    if not found: return
    d["warned"] = sorted(warned | {s["id"] for _, s in found}); cs.save(sid, d)
    lines = []
    for kind, s in found:
        who = f"\"{cs.title(s)[:80]}\" ({s.get('state') or 'live'})"
        if kind == "tree":
            lines.append(f"another live Claude session works in this same folder ({me['root']}): {who}. Edits from both sessions land "
                         "in the same files. Check `git status` before editing; never revert, stash, reset or commit changes you didn't "
                         "make. If your work will touch the same files, tell the user and suggest moving it to its own worktree "
                         "(`claude --worktree <name>`).")
        else:
            lines.append(f"another live Claude session is on the same branch ({me['branch']}) in another checkout of this repo: {who}. "
                         "Pull before you push and don't force-push.")
    msg = "Session board: " + " Also: ".join(lines)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": name, "additionalContext": msg}}))

if __name__ == "__main__":
    try: main()
    except Exception: pass      # never get in the session's way
