#!/usr/bin/env python3
"""Session board - test guard. Synchronous Stop hook: before a session hands back, look at what THIS session changed
(see csb_session.py; older, already-approved changes on the branch don't count) and block once if it removed test cases
overall or added a bare skip/only to a test file, so Claude must justify or undo it.
A skip that carries a reason (skipif(..., reason=...), pytest.skip("..."), @Disabled("...")) is allowed; tests moved
to a new file, tracked or not, balance out. Never blocks twice in a row (stop_hook_active), never errors out."""
import hashlib, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import csb_session as cs

TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)/|(_test|\.test|\.spec|_spec)\.[a-z]+$|(^|/)test_[^/]+\.py$", re.I)
ALWAYS = re.compile(r"(?<![\w.])(it|test|describe|context|suite)\.(skip|only)\(|(?<![\w.])(x|f)(it|describe|test)\(")      # not model.fit( or qs.only(
REASONED = re.compile(r"@pytest\.mark\.(skip|skipif|xfail)\b|\bpytest\.(skip|xfail)\(|@unittest\.skip\w*|\bself\.skipTest\(|"
                      r"\bt\.Skip(Now|f)?\(|@Disabled\b|\[Ignore\b")
TEST_DEF = re.compile(r"^\s*(def test_|async def test_|(it|test)\(\s*['\"`]|func Test[A-Z])")

def in_string(text, pos):
    """is text[pos] inside a quoted string on this line? (a test that writes skip markers into a fixture isn't skipping)"""
    q = None; i = 0
    while i < pos:
        c = text[i]
        if q and c == "\\": i += 2; continue
        if q: q = None if c == q else q
        elif c in "'\"`": q = c
        i += 1
    return q is not None

def bare_skip(text):
    """the line adds a skip/only that gives no reason"""
    if any(not in_string(text, m.start()) for m in ALWAYS.finditer(text)): return True
    m = next((m for m in REASONED.finditer(text) if not in_string(text, m.start())), None)
    if not m: return False
    rest = text[m.end():]
    return not re.search(r"""reason\s*=|^\s*\(?\s*[^)]*['"]""", rest)

def main():
    ev = json.load(sys.stdin)
    if ev.get("stop_hook_active") or ev.get("agent_id"): return
    sid = str(ev.get("session_id") or "x"); cs.budget(12)       # the hook has 15 s
    ch = cs.changes(sid, ev.get("cwd") or "")
    if not ch: return
    tests = [p for _, p in ch["files"] if TEST_PATH.search(p)]
    if not tests: return
    deleted = [p for s, p in ch["files"] if s == "D" and p in tests]
    added_skip, removed_defs, added_defs = [], 0, 0
    for path, line in cs.diff_lines(ch, tests):
        if line.startswith("+") and bare_skip(line[1:]): added_skip.append(f"{path}: {line[1:].strip()[:80]}")
        if TEST_DEF.search(line[1:]):
            if line.startswith("-"): removed_defs += 1
            else: added_defs += 1
    issues = [f"added skip/only without a reason: {x}" for x in added_skip[:5]]
    if removed_defs > added_defs:
        issues.append(f"{removed_defs - added_defs} test case(s) removed overall" + (f" (deleted: {', '.join(deleted[:5])})" if deleted else ""))
    if not issues: return
    # block once per distinct problem: after the user has been told, the same finding doesn't block every later turn
    fp = hashlib.sha1("|".join(sorted(issues)).encode()).hexdigest()
    d = cs.load(sid); seen = d.get("test_guard") or []
    if fp in seen: return
    d["test_guard"] = seen + [fp]; cs.save(sid, d)
    print(json.dumps({"decision": "block", "reason":
        "Test guard: this session's change weakens the test suite (" + "; ".join(issues) + "). Do not hand back like this. "
        "Either restore the tests, or if a test is genuinely wrong, explain exactly why to the user and ask before removing or skipping it."}))

if __name__ == "__main__":
    try: main()
    except Exception: pass
