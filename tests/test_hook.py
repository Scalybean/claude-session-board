"""board/csb-hook.py (macOS/Linux/WSL) against the same event stream as board/hook.ps1 (Windows).
The state tests always run. The parity test also runs hook.ps1 through powershell.exe when it can be reached
(Windows or WSL) and requires both to write the same session record."""
import importlib.machinery, importlib.util, json, os, pathlib, shutil, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY_HOOK = os.path.join(ROOT, "board", "csb-hook.py")
PS_HOOK = os.path.join(ROOT, "board", "hook.ps1")
SID = "11111111-2222-3333-4444-555555555555"

TRANSCRIPT = "\n".join(json.dumps(x) for x in [
    {"type": "user", "isSidechain": False, "timestamp": "2026-10-19T10:00:00.000Z", "message": {"role": "user", "content": "please fix the export bug"}},
    {"type": "assistant", "isSidechain": False, "timestamp": "2026-10-19T10:00:05.000Z", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "I'll look at the exporter first.\n\nThen run the tests."},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "cd /repo && pytest -q", "description": "tests"}}]}},
    {"type": "ai-title", "aiTitle": "Fix CSV export bug"},
]) + "\n"

def events(tr):
    base = {"session_id": SID, "cwd": "/repo", "transcript_path": tr}
    return [
        dict(base, hook_event_name="SessionStart", source="startup", model="claude-sonnet-5-5"),
        dict(base, hook_event_name="UserPromptSubmit", prompt="please fix the export bug in the CSV code"),
        dict(base, hook_event_name="PostToolUse", tool_name="Bash", tool_input={"command": "cd /repo && FOO=1 pytest -q"}),
        dict(base, hook_event_name="PostToolUse", tool_name="TaskCreate", tool_input={"subject": "Find the bug", "activeForm": "Finding"}, tool_response={"task": {"id": "1"}}),
        dict(base, hook_event_name="PostToolUse", tool_name="TaskUpdate", tool_input={"taskId": "1", "status": "in_progress"}),
        dict(base, hook_event_name="PermissionRequest", tool_name="AskUserQuestion", tool_input={"questions": [{"question": "Which file?"}]}),
        dict(base, hook_event_name="Stop", last_assistant_message="Fixed it.\n\nAll tests pass."),
    ]

# fields that depend on the clock or on the process tree
VOLATILE = {"t", "createdAt", "lastEventAt", "stateSince", "endedAt", "tlAt", "pid", "pidStarted", "term"}

def run(cmd, ev, env):
    subprocess.run(cmd, input=json.dumps(ev).encode(), env=env, check=True, capture_output=True, timeout=60)

def session(d):
    with open(os.path.join(d, "sessions", SID + ".json"), encoding="utf-8") as f: return json.load(f)

def stable(s):
    return {k: v for k, v in s.items() if k not in VOLATILE}

class PythonHook(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.tr = os.path.join(self.d, "t.jsonl"); pathlib.Path(self.tr).write_text(TRANSCRIPT)
        self.env = dict(os.environ, CSB_DIR=os.path.join(self.d, "board"))
        self.env.pop("CSB_WSL_PID", None)
    def feed(self, evs):
        for ev in evs: run([sys.executable, PY_HOOK], ev, self.env)
        return session(self.env["CSB_DIR"])
    def test_full_turn(self):
        s = self.feed(events(self.tr)[:-1])
        self.assertEqual(s["state"], "needs"); self.assertEqual(s["detail"], "Asks: Which file?")
        self.assertEqual(s["tasks"], [{"id": "1", "subject": "Find the bug", "active": "Finding", "status": "in_progress"}])
        self.assertEqual(s["aiTitle"], "Fix CSV export bug")
        self.assertEqual([i["k"] for i in s["timeline"]], ["you", "say", "act"])
        self.assertEqual(s["timeline"][2]["x"], "Ran pytest -q")
        s = self.feed(events(self.tr)[-1:])
        self.assertEqual((s["state"], s["lastReply"]), ("yourturn", "Fixed it. All tests pass."))
        self.assertTrue(os.path.isfile(os.path.join(self.env["CSB_DIR"], "sessions.js")))
    def test_subagent_events_are_ignored(self):
        run([sys.executable, PY_HOOK], dict(events(self.tr)[0], agent_id="a1"), self.env)
        self.assertFalse(os.path.exists(os.path.join(self.env["CSB_DIR"], "sessions", SID + ".json")))
    def test_bad_input_never_fails(self):
        r = subprocess.run([sys.executable, PY_HOOK], input=b"not json", env=self.env, capture_output=True)
        self.assertEqual(r.returncode, 0)
    def test_session_end_and_late_notification(self):
        base = events(self.tr)[0]
        s = self.feed([base, dict(base, hook_event_name="SessionEnd"), dict(base, hook_event_name="Stop")])
        self.assertEqual(s["state"], "ended")

    def test_wsl_sessions_record_no_pid_and_foreign_pids_are_not_judged(self):
        env = dict(self.env, WSL_DISTRO_NAME="Ubuntu", CSB_WSL_PID="4242")
        for ev in events(self.tr)[:2]: run([sys.executable, PY_HOOK], ev, env)
        s = session(self.env["CSB_DIR"]); self.assertNotIn("pid", s); self.assertEqual(s["term"], "wsl:Ubuntu:4242")
        win = dict(s, id="win-1", term="win:123", pid=99999999, pidStarted=1, state="working")        # a Windows session on the same board
        path = os.path.join(self.env["CSB_DIR"], "sessions", "win-1.json")
        pathlib.Path(path).write_text(json.dumps(win))
        run([sys.executable, PY_HOOK], events(self.tr)[2], env)
        self.assertEqual(json.loads(pathlib.Path(path).read_text())["state"], "working")
    def test_surrogate_pairs_merge_and_never_stop_the_write(self):
        loader = importlib.machinery.SourceFileLoader("csb_hook", PY_HOOK); spec = importlib.util.spec_from_loader("csb_hook", loader)
        sys.path.insert(0, os.path.join(ROOT, "board")); m = importlib.util.module_from_spec(spec); loader.exec_module(m)
        self.assertEqual(m.unescape_lite("hi \\ud83d\\ude00 there"), "hi \U0001F600 there")
        self.assertNotIn("\ud83d", m.unescape_lite("lone \\ud83d half"))
        s = self.feed([events(self.tr)[0], dict(events(self.tr)[1], prompt="emoji \ud83d here")])      # a lone surrogate in the hook input
        self.assertEqual(s["state"], "working")

@unittest.skipUnless(shutil.which("powershell.exe") or shutil.which("powershell"), "PowerShell not reachable")
class Parity(unittest.TestCase):
    def test_same_record_as_hook_ps1(self):
        ps = shutil.which("powershell.exe") or shutil.which("powershell")
        wsl = os.path.exists("/mnt/c/Windows")
        root = tempfile.mkdtemp(dir="/mnt/c/Users/Public" if wsl else None)
        self.addCleanup(shutil.rmtree, root, True)
        tr = os.path.join(root, "t.jsonl"); pathlib.Path(tr).write_text(TRANSCRIPT)
        def winpath(p):
            return subprocess.run(["wslpath", "-w", p], capture_output=True, text=True).stdout.strip() if wsl else p
        dp, dq = os.path.join(root, "py"), os.path.join(root, "ps")
        env_py = dict(os.environ, CSB_DIR=dp); env_py.pop("CSB_WSL_PID", None)
        env_ps = dict(os.environ, CSB_DIR=winpath(dq), WSLENV=os.environ.get("WSLENV", "") + ":CSB_DIR")
        for ev in events(tr):
            run([sys.executable, PY_HOOK], ev, env_py)
            run([ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", winpath(PS_HOOK)], dict(ev, transcript_path=winpath(tr)), env_ps)
        a, b = stable(session(dp)), stable(session(dq))
        self.assertEqual(a, b)

if __name__ == "__main__":
    unittest.main()
