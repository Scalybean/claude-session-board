"""Keeps the session-board folder from growing forever. Run by the board at start, at most once a day.

Removes, by default (override in <session-board>/ct.json as {"prune": {"days": 14, "backups": 3, "history_months": 6}}):
- per-session hook state older than `days`: plans/.state/*, baseline/*, leftover *.tmp files (here and in plans/, and the
  older /tmp/csb-test-guard-* and /tmp/csb-review-gate-* markers)
- old `backup-*` copies from earlier installs, keeping the newest `backups`
- history/<YYYY-MM>.jsonl months older than `history_months`
Step lists (plans/*.md), sessions/ and setup-backup/ are never touched here."""
import glob, json, os, re, shutil, tempfile, time

DEFAULTS = {"days": 14, "backups": 3, "history_months": 6}

def settings(base):
    try:
        with open(os.path.join(base, "ct.json"), encoding="utf-8") as f: p = json.load(f).get("prune") or {}
    except Exception:
        p = {}
    return {k: int(p.get(k, v)) if str(p.get(k, v)).isdigit() else v for k, v in DEFAULTS.items()}

def _rm(p):
    try:
        if os.path.isdir(p) and not os.path.islink(p): shutil.rmtree(p)
        else: os.remove(p)
        return True
    except OSError:
        return False

def prune(base, now=None, force=False):
    """returns the paths removed. Does nothing if it already ran in the last 24 h (unless force)."""
    now = now or time.time()
    mark = os.path.join(base, ".pruned")
    if not force and os.path.exists(mark) and now - os.path.getmtime(mark) < 86400: return []
    cfg = settings(base); old = now - cfg["days"] * 86400; gone = []
    stale = glob.glob(os.path.join(base, "plans", ".state", "*")) + glob.glob(os.path.join(base, "baseline", "*.json")) \
          + glob.glob(os.path.join(base, "*.tmp")) + glob.glob(os.path.join(base, "plans", "*.tmp")) \
          + glob.glob(os.path.join(tempfile.gettempdir(), "csb-test-guard-*")) + glob.glob(os.path.join(tempfile.gettempdir(), "csb-review-gate-*"))
    for p in stale:
        try:
            if os.path.getmtime(p) < old and _rm(p): gone.append(p)
        except OSError:
            pass
    stamp = lambda p: re.sub(r"\D", "", re.split(r"backup-|\.bak-", os.path.basename(p), 1)[-1])[:14]
    backups = sorted(glob.glob(os.path.join(base, "backup-*")) + glob.glob(os.path.join(base, "*.bak-*")), key=stamp)
    for p in backups[:max(0, len(backups) - cfg["backups"])]:       # oldest first, by the date in the name
        if _rm(p): gone.append(p)
    t = time.localtime(now); months = t.tm_year * 12 + t.tm_mon - 1 - cfg["history_months"]
    cutoff = "%04d-%02d" % (months // 12, months % 12 + 1)
    for p in glob.glob(os.path.join(base, "history", "*.jsonl")):
        if os.path.basename(p)[:7] < cutoff and _rm(p): gone.append(p)
    try:
        with open(mark, "w"): pass
    except OSError:
        pass
    return gone
