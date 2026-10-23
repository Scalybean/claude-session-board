#!/usr/bin/env python3
"""Session board - review gate. Synchronous Stop hook: a routine or risky task packet (tasks/<slug>.md) can only be
marked review/done once it records an independent reviewer verdict ("reviewer: pass|fixed|..." line) and evidence
("evidence:" line). Otherwise the hand-back is blocked once with instructions. Quick tasks have no packet, so no gate.
Only packets THIS session changed are checked (see csb_session.py), and lines inside ``` code blocks don't count."""
import glob, hashlib, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import csb_session as cs

def packets(sid, cwd):
    """tasks/*.md files this session touched: from git, or outside git by modification time since the session started"""
    ch = cs.changes(sid, cwd)
    if ch:
        return [os.path.join(ch["root"], p) for s, p in ch["files"] if s != "D" and re.match(r"^(.*/)?tasks/[^/]+\.md$", p)]
    cwd = cs.to_local(cwd)
    start = (cs.load(sid).get("repos", {}).get(os.path.normpath(cwd)) or {}).get("at")
    if not start: return []
    return [f for f in glob.glob(os.path.join(cwd, "tasks", "*.md")) if os.path.getmtime(f) >= start]

def main():
    ev = json.load(sys.stdin)
    if ev.get("stop_hook_active") or ev.get("agent_id"): return
    sid, cwd = str(ev.get("session_id") or "x"), ev.get("cwd") or ""; cs.budget(7)
    missing = []
    for f in packets(sid, cwd):
        if os.path.basename(f).upper() == "PROGRESS.MD": continue
        try: txt = open(f, encoding="utf-8").read()
        except Exception: continue
        txt = re.sub(r"^(```|~~~).*?^\1[^\n]*$", "", txt, flags=re.M | re.S)     # examples in code blocks aren't the packet's own fields
        tier = re.search(r"^tier:\s*(\w+)", txt, re.M); st = re.search(r"^status:\s*([\w-]+)", txt, re.M)
        if not tier or tier.group(1) not in ("routine", "risky") or not st or st.group(1) not in ("review", "done"): continue
        need = [k for k in ("reviewer", "evidence") if not re.search(rf"^{k}:\s*\S", txt, re.M | re.I)]
        if need: missing.append(f"{os.path.relpath(f, cs.to_local(cwd) or '.')} (no {' / no '.join(need)} line)")
    if not missing: return
    fp = hashlib.sha1("|".join(missing).encode()).hexdigest()
    d = cs.load(sid); seen = d.get("review_gate") or []
    if fp in seen: return
    d["review_gate"] = seen + [fp]; cs.save(sid, d)
    print(json.dumps({"decision": "block", "reason":
        "Review gate: " + "; ".join(missing) + ". A routine/risky task can't be marked review/done until an independent reviewer has "
        "checked it. Spawn a fresh reviewer (/code-review, or a subagent given only `git diff <base>...HEAD` plus the packet's goal and "
        "acceptance, told to flag only correctness or requirement gaps), fix or answer its findings, then add to the packet "
        "`reviewer: <pass | fixed: ... | open: ...>` and `evidence: <checks run and their result>`."}))

if __name__ == "__main__":
    try: main()
    except Exception: pass
