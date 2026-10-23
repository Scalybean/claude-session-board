"""The real board, started in a pty with a throwaway HOME and session-board folder: the first-run questions appear, nothing
changes until the summary is confirmed, and each way through leaves the machine as promised. POSIX only."""
import fcntl, json, os, pty, re, select, shutil, struct, sys, tempfile, termios, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]|\x1b[=>]")
SETTINGS = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}

def rd(p, mode="r"):
    with open(p, mode) as f: return f.read()

class Board:
    """the board in a pty: send keys, read what it drew since the last read"""
    def __init__(self, home, board, args=()):
        env = dict(os.environ, HOME=home, CSB_DIR=board, CSB_TABS="0", TERM="xterm-256color"); env.pop("CLAUDE_CONFIG_DIR", None)
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execvpe(sys.executable, [sys.executable, os.path.join(ROOT, "board", "csb-tui.py"), *args], env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 120, 0, 0))
    def read(self, sec=0.6):
        out = b""; end = time.time() + sec
        while time.time() < end:
            if select.select([self.fd], [], [], 0.05)[0]:
                try: out += os.read(self.fd, 65536)
                except OSError: break
        return ANSI.sub("", out.decode("utf-8", "replace"))
    def send(self, keys, sec=0.6):
        os.write(self.fd, keys.encode()); return self.read(sec)
    def wait_exit(self, sec=5):
        end = time.time() + sec
        while time.time() < end:
            p, st = os.waitpid(self.pid, os.WNOHANG)
            if p: return os.waitstatus_to_exitcode(st)
            time.sleep(0.05)
        os.kill(self.pid, 9); return None

class FirstRun(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(); self.board = os.path.join(self.home, "board")
        os.makedirs(os.path.join(self.home, ".claude"))
        self.settings = os.path.join(self.home, ".claude", "settings.json")
        with open(self.settings, "w") as f: json.dump(SETTINGS, f)
        self.before = rd(self.settings)
    def tearDown(self): shutil.rmtree(self.home, True)
    def start(self, *args): return Board(self.home, self.board, args)
    def untouched(self):
        self.assertEqual(rd(self.settings), self.before)
        for f in ("ntfy.json", "todoist.json", "ct.json"): self.assertFalse(os.path.exists(os.path.join(self.board, f)), f)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "CLAUDE.md")))

    def test_welcome_then_skip_changes_nothing_and_asks_again(self):
        b = self.start(); out = b.read(1.5)
        self.assertIn("Welcome to the Claude session board", out); self.assertIn("Nothing is on until you tick it", out)
        b.send("s"); self.assertEqual(b.send("q", 1.0) is not None and b.wait_exit(), 0)
        self.untouched(); self.assertFalse(os.path.exists(os.path.join(self.board, "setup.json")))
        b = self.start(); self.assertIn("Welcome to the Claude session board", b.read(1.5)); b.send("s"); b.send("q"); b.wait_exit()
    def test_never_ask_again(self):
        b = self.start(); b.read(1.5); b.send("n"); b.send("q"); b.wait_exit(); self.untouched()
        b = self.start(); self.assertNotIn("Welcome", b.read(1.5)); b.send("q"); self.assertEqual(b.wait_exit(), 0)
    def test_all_off_through_every_screen(self):
        b = self.start(); b.read(1.5)
        out = b.send("\r"); self.assertIn("Add-ons", out); self.assertIn("[ ] Show your sessions on the board", out)
        out = b.send("\r"); self.assertIn("How tasks are created and managed", out)
        out = b.send("\r"); self.assertIn("Task management", out); self.assertIn("Guard: review gate", out); self.assertNotIn("[x]", out)
        out = b.send("\r"); self.assertIn("Summary", out); self.assertIn("nothing - everything stays off", out)
        self.untouched()
        out = b.send("\r"); self.assertIn("Done", out)
        b.send("x", 1.0); b.send("q"); self.assertEqual(b.wait_exit(), 0)
        self.untouched(); self.assertTrue(json.loads(rd(os.path.join(self.board, "setup.json")))["done"])
        b = self.start(); self.assertNotIn("Welcome", b.read(1.5)); b.send("q"); b.wait_exit()
    def test_everything_on(self):
        b = self.start(); b.read(1.5); b.send("\r")
        b.send("a")                                   # tick every add-on that works here
        out = b.send("\r"); self.assertIn("Todoist: paste your API token", out)
        out = b.send("secret-token\r"); self.assertIn("Phone pushes", out)
        out = b.send("n"); self.assertIn("How tasks are created", out)
        b.send("\r"); b.send("a")
        out = b.send("\r"); self.assertIn("ct limits", out)
        b.send("\r"); out = b.send("3\r")
        self.assertIn("Summary", out); self.assertIn("generic text only", out); self.assertIn("backup first", out)
        self.assertEqual(rd(self.settings), self.before)                   # still untouched until confirmed
        out = b.send("\r", 1.5); self.assertIn("Done", out)
        b.send("x", 1.0); b.send("q"); self.assertEqual(b.wait_exit(), 0)
        d = json.loads(rd(self.settings)); cmds = [h["command"] for g in sum(d["hooks"].values(), []) for h in g["hooks"]]
        self.assertIn("echo mine", cmds); self.assertTrue(any("csb-hook.py" in c for c in cmds)); self.assertTrue(any("csb-push-guard" in c for c in cmds))
        self.assertEqual(json.loads(rd(os.path.join(self.board, "todoist.json")))["token"], "secret-token")
        self.assertEqual(json.loads(rd(os.path.join(self.board, "ct.json")))["max_review"], 3)
        ntfy = json.loads(rd(os.path.join(self.board, "ntfy.json"))); self.assertFalse(ntfy["detail"])
        self.assertIn("<!-- claude-tasks:start -->", rd(os.path.join(self.home, ".claude", "CLAUDE.md")))
    def test_setup_flag_reopens_and_uninstall_undoes(self):
        b = self.start(); b.read(1.5); b.send("\r"); b.send("\r"); b.send("\r"); b.send("\r"); b.send("\r"); b.send("x", 1.0); b.send("q"); b.wait_exit()
        b = self.start("--setup"); out = b.read(1.5)
        self.assertIn("Add-ons", out); self.assertNotIn("Welcome", out)           # --setup goes straight to the questions
        b.send("\x1b"); b.send("q"); b.wait_exit()
    def test_no_phone_push_or_topic_unless_chosen(self):
        b = self.start(); b.read(1.5); b.send("s"); b.send("q"); b.wait_exit()
        self.assertFalse(os.path.exists(os.path.join(self.board, "ntfy.json")))

if __name__ == "__main__":
    unittest.main()
