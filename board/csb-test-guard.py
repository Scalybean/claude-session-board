#!/usr/bin/env python3
"""Session board - test guard. Synchronous Stop hook: before a session hands back, look at what THIS session changed
(see csb_session.py; older, already-approved changes on the branch don't count) and block once if it removed test cases
overall, added a bare skip/only to a test file, changed or removed an assertion in an existing test (the usual way agents
get to green: edit the expected value to match the bug), or rewrote the acceptance list in tasks/FEATURES.json (agents
may add features and flip `passes`, nothing else). Claude must then justify it to the user or undo it.
A skip that carries a reason (skipif(..., reason=...), pytest.skip("..."), @Disabled("...")) is allowed; tests moved
to a new file, tracked or not, balance out. Never blocks twice in a row (stop_hook_active), never errors out."""
import hashlib, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import csb_session as cs

TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)/|(_test|\.test|\.spec|_spec)\.[a-z]+$|(^|/)test_[^/]+\.py$", re.I)
ALWAYS = re.compile(r"(?<![\w.])(it|test|describe|context|suite)\.(skip|only)\(|(?<![\w.])(x|f)(it|describe|test)\(")      # not model.fit( or qs.only(
REASONED = re.compile(r"@pytest\.mark\.(skip|skipif|xfail)\b|\bpytest\.(skip|xfail)\(|@unittest\.skip\w*|\bself\.skipTest\(|"
                      r"\bt\.Skip(Now|f)?\(|@Disabled\b|\[Ignore\b")
ASSERTION = re.compile(r"\bassert\w*\b|\bexpect\(|\.to(Be|Equal|StrictEqual|Match|Throw|Have|Contain)\w*\(|\.should\.|"
                       r"\bt\.(Error|Fatal)f?\(|\brequire\.\w+\(|\bExpect\(")
COMMENT = re.compile(r"^\s*(#|//|/?\*)")
# a value a test can expect: a number, a quoted string, a bool/null, an empty list or dict. An expected value often sits
# on its own line (a multi-line assertEqual, a parametrize table, `expected = 3`), so these count too, not just assert lines
LITERAL = re.compile(r"(?<![\w.])-?\d+(\.\d+)?\b|['\"`]|\b(True|False|None|true|false|null|nil|undefined)\b|\[\s*\]|\{\s*\}")
NOT_A_CHECK = re.compile(r"^\s*(import |from \S+ import |def |async def |class |func |package |@pytest\.fixture|require\(|const \w+ = require)")
FEATURES = re.compile(r"(^|/)tasks/FEATURES\.json$")
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

def features_issues(ch):
    """features this session removed, or whose title/acceptance it rewrote, in tasks/FEATURES.json"""
    out = []
    for st, path in ch["files"]:
        if not FEATURES.search(path) or path in ch["new"]: continue
        def load(text):
            try:
                d = json.loads(text); d = d.get("features", d) if isinstance(d, dict) else d
                return {str(f.get("id") or f.get("title")): f for f in d if isinstance(f, dict)}
            except Exception:
                return None
        before = load(cs.git(ch["root"], "show", f"{ch['start']}:{path}"))
        if before is None: continue
        try:
            with open(os.path.join(ch["root"], path), encoding="utf-8") as f: after = load(f.read())
        except OSError:
            after = {}
        if after is None: out.append(f"{path} is no longer valid JSON"); continue
        for k, f in before.items():
            g = after.get(k)
            if g is None: out.append(f"feature {k} removed from {path}")
            elif any(f.get(x) != g.get(x) for x in ("title", "acceptance")): out.append(f"feature {k}: its title/acceptance was rewritten")
    return out

def main():
    ev = json.load(sys.stdin)
    if ev.get("stop_hook_active") or ev.get("agent_id"): return
    sid = str(ev.get("session_id") or "x"); cs.budget(12)       # the hook has 15 s
    ch = cs.changes(sid, ev.get("cwd") or "")
    if not ch: return
    tests = [p for _, p in ch["files"] if TEST_PATH.search(p)]
    feats = features_issues(ch)
    if not tests and not feats: return
    deleted = [p for s, p in ch["files"] if s == "D" and p in tests]
    added_skip, removed_defs, added_defs = [], 0, 0
    gone_asserts, added_lines = [], set()
    for path, line in (cs.diff_lines(ch, tests) if tests else []):
        body = line[1:].strip()
        if line.startswith("+"): added_lines.add(body)
        elif body and not COMMENT.match(body) and not NOT_A_CHECK.match(body) and (
                any(not in_string(body, m.start()) for m in ASSERTION.finditer(body)) or LITERAL.search(body)):
            gone_asserts.append((path, body))
        if line.startswith("+") and bare_skip(line[1:]): added_skip.append(f"{path}: {line[1:].strip()[:80]}")
        if TEST_DEF.search(line[1:]):
            if line.startswith("-"): removed_defs += 1
            else: added_defs += 1
    issues = [f"added skip/only without a reason: {x}" for x in added_skip[:5]]
    changed = [f"{p}: {b[:80]}" for p, b in gone_asserts if b not in added_lines]      # a line that only moved is fine
    if changed: issues.append(f"{len(changed)} existing assertion(s) or expected value(s) changed or removed: " + "; ".join(changed[:5]))
    issues += feats
    if removed_defs > added_defs:
        issues.append(f"{removed_defs - added_defs} test case(s) removed overall" + (f" (deleted: {', '.join(deleted[:5])})" if deleted else ""))
    if not issues: return
    # block once per distinct problem: after the user has been told, the same finding doesn't block every later turn
    fp = hashlib.sha1("|".join(sorted(issues)).encode()).hexdigest()
    d = cs.load(sid); seen = d.get("test_guard") or []
    if fp in seen: return
    d["test_guard"] = seen + [fp]; cs.save(sid, d)
    print(json.dumps({"decision": "block", "reason":
        "Test guard: this session's change may weaken the checks (" + "; ".join(issues) + "). Do not hand back like this. "
        "If an expected value changed because the task asked for different behaviour, say so plainly in the packet's decisions and in "
        "your hand-off, then continue. If two tests or checks contradict each other, or a test looks wrong, stop and ask the user "
        "which is right. Never change an expected value, loosen an assertion or rewrite an acceptance check just to get green."}))

if __name__ == "__main__":
    try: main()
    except Exception: pass
