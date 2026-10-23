"""What one Claude Code session changed in its git repo, and which live sessions share its working tree.

The Stop guards judge only the session's own work. At the session's first prompt in a repo, csb-start.py records the
commit it started on, the time, and the hash of every file that was already dirty (`git hash-object` without -w, so
nothing is written into the repo). At Stop, the session's changes are:
- commits made since that time that aren't merges and weren't there at the start (a pull, merge, rebase onto main or a
  checkout brings in other people's older commits, and those keep their old dates, so they don't count), and
- uncommitted changes, minus files that are still exactly as they were at the start.
A repo with no recorded start (the session cd'ed there mid-turn) counts as nothing changed: never blame earlier work.
A file that was dirty at the start and changed again is diffed whole (rare; noted rather than stored).
Baselines live in <session-board>/baseline/<session_id>.json; the board prunes old ones."""
import json, os, re, subprocess, time
from csb_paths import find_dir

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
LIVE_HOURS = 12          # a session silent for longer than this no longer counts as sharing a folder
_deadline = None

def budget(seconds):
    """all git calls from now on share this many seconds, so a slow repo (the Windows drive from WSL) can't push a hook
    past its timeout: when time runs out, git answers nothing and the guards stay quiet"""
    global _deadline
    _deadline = time.time() + seconds

def to_local(p):
    """a Windows path handed to us by a Windows session, as seen from WSL"""
    m = re.match(r"^([A-Za-z]):[\\/](.*)", p or "")
    return "/mnt/" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/") if m and os.name != "nt" else (p or "")

def git(cwd, *a, timeout=10, stdin=None):
    if _deadline is not None: timeout = min(timeout, _deadline - time.time())
    if timeout <= 0: return ""
    try:
        return subprocess.run(["git", "-C", cwd, "-c", "core.quotePath=false", *a], input=stdin, capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace").stdout
    except Exception:
        return ""

def tree_info(cwd):
    """{root, common, branch} of the git working tree at cwd, or None outside git"""
    cwd = to_local(cwd)
    if not cwd or not os.path.isdir(cwd): return None
    out = git(cwd, "rev-parse", "--show-toplevel", "--git-common-dir", "--abbrev-ref", "HEAD", timeout=5).splitlines()
    if len(out) < 2 or not out[0]: return None
    return {"root": os.path.normpath(out[0]), "common": os.path.normpath(os.path.join(cwd, out[1])),
            "branch": out[2] if len(out) > 2 and out[2] != "HEAD" else ""}

# ---------- baseline file ----------
def _file(sid):
    return os.path.join(find_dir(), "baseline", re.sub(r"[^A-Za-z0-9_-]", "_", str(sid)) + ".json")

def load(sid):
    try:
        with open(_file(sid), encoding="utf-8") as f: d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def save(sid, d):
    p = _file(sid); os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(d, f)
    os.replace(tmp, p)

def _status_paths(root):
    """paths git reports as changed or untracked"""
    out = git(root, "status", "--porcelain", "-z", "--untracked-files=all").split("\0")
    paths, i = [], 0
    while i < len(out):
        e = out[i]; i += 1
        if len(e) < 4: continue
        paths.append(e[3:])
        if e[0] in "RC": i += 1          # a rename carries its old path next; the old path no longer exists
    return paths

def _hashes(root, paths):
    """content hash of each path as it is now (None if missing); computed, never written to the repo. --no-filters: no
    clean filters (git-lfs and friends) run, so it's fast and the same every time"""
    have = [p for p in paths if os.path.isfile(os.path.join(root, p))]
    h = {}
    for i in range(0, len(have), 500):
        part = have[i:i + 500]
        got = git(root, "hash-object", "--no-filters", "--stdin-paths", stdin="\n".join(part) + "\n").split()
        if len(got) == len(part): h.update(zip(part, got))
    return {p: h.get(p) for p in paths}

def _head(root):
    return git(root, "rev-parse", "--verify", "-q", "HEAD").strip()

def record(sid, cwd):
    """remember where this session started in the repo at cwd, once per repo (a resume keeps it, so work from before an
    interruption is still this session's). Returns the repo root."""
    info = tree_info(cwd)
    root = info["root"] if info else os.path.normpath(to_local(cwd) or ".")
    d = load(sid); repos = d.setdefault("repos", {})
    if root in repos: return root
    if info:
        repos[root] = {"head": _head(root) or EMPTY_TREE, "dirty": _hashes(root, _status_paths(root)), "at": int(time.time())}
    else:
        repos[root] = {"head": None, "dirty": {}, "at": int(time.time())}
    save(sid, d)
    return root

def changes(sid, cwd):
    """what this session changed in the repo at cwd: {root, head, commits, files: [(status, path)], uncommitted, new}.
    None outside git, or when the session has no recorded start in this repo."""
    info = tree_info(cwd)
    if not info: return None
    root = info["root"]; b = load(sid).get("repos", {}).get(root)
    if not b or not b.get("head"): return None
    head = _head(root)
    files, commits = {}, []
    if head:
        since = "--since=@%d" % (int(b.get("at") or 0) - 60)          # a minute of slack for clock rounding
        start = b["head"] if b["head"] != EMPTY_TREE and git(root, "cat-file", "-t", b["head"]).strip() == "commit" else None
        commits = git(root, "rev-list", "--no-merges", since, "--max-count=100", "HEAD", *(["^" + start] if start else [])).split()
        if commits:
            for line in git(root, "show", "--no-renames", "--name-status", "--format=", *commits).splitlines():
                st, _, path = line.partition("\t")
                if path: files[path] = st[:1]
    out = git(root, "diff", "--name-status", "-z", "--no-renames", head or EMPTY_TREE).split("\0")
    unc = {out[i + 1]: out[i][:1] for i in range(0, len(out) - 1, 2) if out[i]}
    new = set(p for p in git(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0") if p)
    unc.update({p: "A" for p in new})
    start_dirty = b.get("dirty") or {}
    if start_dirty:
        now = _hashes(root, [p for p in unc if p in start_dirty])
        unc = {p: st for p, st in unc.items() if p not in start_dirty or now.get(p) != start_dirty[p]}
    files.update(unc)
    return {"root": root, "head": head or EMPTY_TREE, "commits": commits, "files": sorted((st, p) for p, st in files.items()),
            "uncommitted": set(unc), "new": new & set(unc)}

def _diff_text(text):
    old = cur = None; head = False               # file headers come between "diff --git" and the first "@@"
    for line in text.splitlines():
        if line.startswith("diff --git "): head = True; cur = None; continue
        if line.startswith("@@"): head = False; continue
        if head:
            if line.startswith("--- "): old = line[6:] if line.startswith("--- a/") else None
            elif line.startswith("+++ "): cur = line[6:] if line.startswith("+++ b/") else old
            continue
        if cur and line[:1] in "+-": yield cur, line

def diff_lines(ch, paths):
    """(path, line) for every added/removed line of these changed paths: each of the session's commits, then what is
    uncommitted; new untracked files count as all added. A change undone in a later commit balances out."""
    paths = list(paths)
    for i in range(0, len(ch["commits"]), 50):
        yield from _diff_text(git(ch["root"], "show", "-U0", "--no-renames", "--no-color", "--format=", *ch["commits"][i:i + 50], "--", *paths))
    tracked = [p for p in paths if p in ch["uncommitted"] and p not in ch["new"]]
    for i in range(0, len(tracked), 200):
        yield from _diff_text(git(ch["root"], "diff", "-U0", "--no-renames", "--no-color", ch["head"], "--", *tracked[i:i + 200]))
    for p in sorted(set(paths) & ch["new"]):
        try:
            with open(os.path.join(ch["root"], p), encoding="utf-8", errors="replace") as f: text = f.read(1_000_000)
        except OSError:
            continue
        for line in text.splitlines(): yield p, "+" + line

# ---------- live sessions sharing a working tree ----------
def live_sessions(now=None):
    now = now or time.time()
    out = []
    for f in os.listdir(os.path.join(find_dir(), "sessions")) if os.path.isdir(os.path.join(find_dir(), "sessions")) else []:
        if not f.endswith(".json"): continue
        try:
            with open(os.path.join(find_dir(), "sessions", f), encoding="utf-8-sig") as fh: s = json.load(fh)
        except Exception:
            continue
        if isinstance(s, dict) and s.get("state") != "ended" and s.get("cwd") and s.get("surface") not in ("chat", "cloud") \
                and now * 1000 - int(s.get("lastEventAt") or 0) < LIVE_HOURS * 3600000:
            out.append(s)
    return sorted(out, key=lambda s: -int(s.get("lastEventAt") or 0))[:20]

def overlap(a, b):
    """'tree' when two tree_infos are the same working tree, 'branch' when they are the same branch of one repo, else None"""
    if not a or not b: return None
    if a["root"] == b["root"]: return "tree"
    if a["common"] == b["common"] and a["branch"] and a["branch"] == b["branch"]: return "branch"
    return None

def title(s):
    return s.get("name") or s.get("aiTitle") or s.get("topic") or s.get("firstPrompt") or s.get("id", "")[:8]
