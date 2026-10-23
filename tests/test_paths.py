"""The data-folder resolver: board/csb_paths.py and the copy inside bin/ct must give the same answer in every case."""
import glob, importlib.machinery, importlib.util, os, sys, tempfile, unittest, unittest.mock
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "board"))
import csb_paths

def load_ct():
    loader = importlib.machinery.SourceFileLoader("ct", os.path.join(ROOT, "bin", "ct"))
    spec = importlib.util.spec_from_loader("ct", loader); m = importlib.util.module_from_spec(spec); loader.exec_module(m)
    return m

class Paths(unittest.TestCase):
    def setUp(self):
        self.ct = load_ct(); self.env = dict(os.environ)
        self.home = tempfile.mkdtemp(); os.environ["HOME"] = self.home; os.environ.pop("CSB_DIR", None)
        # a real Windows-side board (this is often run on WSL) must not leak into the cases below
        real = glob.glob
        self.patch = unittest.mock.patch("glob.glob", lambda p, *a, **k: [] if p.startswith("/mnt/c/") else real(p, *a, **k))
        self.patch.start()
    def tearDown(self):
        self.patch.stop(); os.environ.clear(); os.environ.update(self.env)
    def both(self):
        a, b = csb_paths.find_dir(), self.ct.board_dir(); self.assertEqual(a, b); return a
    def test_env_wins(self):
        os.environ["CSB_DIR"] = "/somewhere/else"; self.assertEqual(self.both(), "/somewhere/else")
    def test_default_is_home(self):
        self.assertEqual(self.both(), os.path.join(self.home, ".claude", "session-board"))
    def test_wsl_uses_windows_side_only_when_a_board_runs_there(self):
        win = os.path.join(self.home, "winhome", ".claude", "session-board")
        os.makedirs(os.path.join(win, "sessions"))
        orig = csb_paths.windows_homes
        csb_paths.windows_homes = lambda: [os.path.join(self.home, "winhome")]
        try: self.assertEqual(csb_paths.find_dir(), win)
        finally: csb_paths.windows_homes = orig
    def test_no_windows_paths_outside_wsl(self):
        if not csb_paths.is_wsl(): self.assertEqual(csb_paths.windows_homes(), [])

if __name__ == "__main__":
    unittest.main()
