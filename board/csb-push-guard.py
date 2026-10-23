#!/usr/bin/env python3
"""Session board - push guard. Synchronous PreToolUse hook (Bash/PowerShell): a `git push` that updates or deletes
main/master (or the repo's default branch) asks you first, even in auto mode. Task work goes to its own branch and a PR.
The command is split into its parts (&&, ||, ;, |, newlines, subshell brackets) and only a real `git ... push` part is
looked at, in the folder it runs in (`cd <dir>` before it, `git -C <dir>`), also behind sudo/env/timeout/command and
inside `bash -c "..."`. Quoted text that only mentions git push is left alone, and so are dry runs and pushes of
tags only."""
import fnmatch, json, os, re, shlex, subprocess, sys, time

OPS = {"&&", "||", ";", "|", "&", ";;", "|&", "(", ")", "{", "}"}
TAKES_VALUE = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
WRAPPERS = {"sudo", "command", "builtin", "exec", "nohup", "time", "env", "nice", "timeout", "stdbuf", "ionice", "doas"}
WRAPPER_VALUE = {"-u", "-g", "-n", "-s", "-k", "-c", "-p", "-C", "--user", "--group", "--signal", "--kill-after"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
DEADLINE = time.time() + 7         # the hook has 10 s; git on the Windows drive can be slow

def to_local(p):
    m = re.match(r"^([A-Za-z]):[\\/](.*)", p or "")
    return "/mnt/" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/") if m and os.name != "nt" else p

def git(cwd, *a):
    left = min(5, DEADLINE - time.time())
    if left <= 0: return ""
    try: return subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True, timeout=left).stdout.strip()
    except Exception: return ""

def lines(cmd):
    """split on newlines that are outside quotes (a quoted commit message may span lines)"""
    out, cur, q, esc = [], [], None, False
    for c in cmd.replace("\r", ""):
        if esc: esc = False
        elif c == "\\" and q != "'": esc = True
        elif q: q = None if c == q else q
        elif c in "'\"": q = c
        elif c == "\n": out.append("".join(cur)); cur = []; continue
        cur.append(c)
    return out + ["".join(cur)]

def parts(cmd):
    """the command's simple commands, as token lists"""
    out = []
    for line in lines(cmd):
        try:
            lx = shlex.shlex(line, posix=True, punctuation_chars=";&|()"); lx.whitespace_split = True; lx.commenters = ""
            toks = list(lx)
        except ValueError:                 # unbalanced quotes: fall back to plain words
            toks = line.split()
        cur = []
        for t in toks:
            if t in OPS:
                if cur: out.append(cur)
                cur = []
            else: cur.append(t)
        if cur: out.append(cur)
    return out

def push_targets(args, cwd):
    """branch names (or patterns) a `git push <args>` would update or delete; [] when it moves none"""
    if any(a in ("--dry-run", "-n") for a in args): return []
    if any(a in ("--all", "--mirror", "--branches") for a in args): return ["*"]
    pos, skip = [], False
    for a in args:
        if skip: skip = False; continue
        if a in TAKES_VALUE: skip = True; continue
        if a.startswith("-"): continue
        pos.append(a)
    refs = pos[1:]
    if not refs:
        if "--tags" in args or any(a in ("-d", "--delete") for a in args): return []     # tags only / nothing named to delete
        # plain `git push` / `git push origin`: the branch it tracks, else the current one
        up = git(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        cur = git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        return [up.split("/", 1)[-1] if up else cur]
    out = []
    for r in refs:                          # with --delete every name is a branch to delete: deleting main asks too
        dst = r.lstrip("+").split(":")[-1]
        if dst.startswith("refs/tags/"): continue
        dst = dst.replace("refs/heads/", "")
        out.append(git(cwd, "rev-parse", "--abbrev-ref", "HEAD") if dst in ("HEAD", "@") else dst)
    return out

def unwrap(p):
    """drop VAR=value and wrappers (sudo -u x, env A=1, timeout 60, command, ...) in front of the real command"""
    while p:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", p[0]): p = p[1:]; continue
        w = os.path.basename(p[0])
        if w not in WRAPPERS: break
        p = p[1:]
        while p and (p[0].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", p[0])):
            p = p[2:] if p[0] in WRAPPER_VALUE else p[1:]
        if w == "timeout" and p: p = p[1:]          # its duration
    return p

def protected_hits(cmd, cwd, depth=0):
    """(protected branches hit, folder) for the first push in cmd that updates one; ([], _) if none"""
    for p in parts(cmd):
        if p[0] in ("cd", "pushd", "Set-Location", "sl") and len(p) > 1:
            cwd = os.path.join(cwd, os.path.expanduser(to_local(p[-1]))); continue
        p = unwrap(p)
        if not p: continue
        exe = os.path.basename(p[0]).lower()
        if exe in SHELLS and "-c" in p[1:-1] and depth < 3:          # bash -c "git push origin main"
            hit = protected_hits(p[p.index("-c", 1) + 1], cwd, depth + 1)
            if hit[0]: return hit
            continue
        if exe not in ("git", "git.exe"): continue
        here, i = cwd, 1
        while i < len(p) and p[i].startswith("-"):     # git's own options before the subcommand
            if p[i] == "-C" and i + 1 < len(p): here = os.path.join(here, os.path.expanduser(to_local(p[i + 1]))); i += 2
            elif p[i] in ("-c", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(p): i += 2
            else: i += 1
        if i >= len(p) or p[i] != "push": continue
        protected = {"main", "master"}
        d = git(here, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        if d: protected.add(d.split("/", 1)[-1])
        hit = sorted({b for t in push_targets(p[i + 1:], here) for b in protected if fnmatch.fnmatchcase(b, t)})
        if hit: return hit, here
    return [], cwd

def main():
    ev = json.load(sys.stdin)
    cmd = (ev.get("tool_input") or {}).get("command") or ""
    if not re.search(r"\bpush\b", cmd): return
    hit, _ = protected_hits(cmd, to_local(ev.get("cwd") or ""))
    if hit:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
            "permissionDecisionReason": f"Push to {', '.join(hit)} (protected branch). Task work should go to its own branch and a PR."}}))

if __name__ == "__main__":
    try: main()
    except Exception: pass
