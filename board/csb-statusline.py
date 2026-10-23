#!/usr/bin/env python3
"""Claude Code statusLine command: saves plan usage for the session board, prints a short status line.
Claude Code pipes a JSON document to stdin on every update; rate_limits is only there for Pro/Max subscribers.
Writes <session-board>/usage.json (latest rate_limits seen + when)."""
import glob, json, os, sys, time
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
from csb_paths import find_dir
base = find_dir()
rl = d.get("rate_limits") or {}
if rl and os.path.isdir(base):
    # Claude Code ends a status-line run when a newer one starts: turn that SIGTERM into a normal exit so the
    # temp file is removed instead of being left behind
    import signal
    try: signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    except Exception: pass
    tmp = os.path.join(base, "usage.json.%d.tmp" % os.getpid())
    text = json.dumps({"at": int(time.time()), "rate_limits": rl, "session": d.get("session_id")})
    try:
        with open(tmp, "w", encoding="utf-8") as fh: fh.write(text)
        os.replace(tmp, os.path.join(base, "usage.json"))
    except Exception:
        pass
    finally:
        try: os.remove(tmp)
        except OSError: pass
cw = d.get("context_window") or {}
parts = [(d.get("model") or {}).get("display_name") or ""]
if cw.get("used_percentage") is not None: parts.append("ctx %d%%" % cw["used_percentage"])
f = (rl.get("five_hour") or {}).get("used_percentage")
if f is not None: parts.append("5h %d%%" % f)
print(" · ".join(p for p in parts if p))
