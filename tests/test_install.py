"""install.py into a throwaway HOME / session-board folder: only files are copied, nothing else is edited, and the
installed copy (not the checkout) runs setup and finds everything it ships."""
import json, os, shutil, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def rd(p, mode="r"):
    with open(p, mode) as f: return f.read()

class Install(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(); self.board = os.path.join(self.home, "board")
        os.makedirs(os.path.join(self.home, ".claude"))
        self.env = dict(os.environ, HOME=self.home, CSB_DIR=self.board); self.env.pop("CLAUDE_CONFIG_DIR", None)
        with open(os.path.join(self.home, ".claude", "settings.json"), "w") as f: f.write('{"model": "opus"}')
        with open(os.path.join(self.home, ".claude", "CLAUDE.md"), "w") as f: f.write("mine\n")
    def tearDown(self): shutil.rmtree(self.home, True)
    def install(self):
        return subprocess.run([sys.executable, os.path.join(ROOT, "install.py")], env=self.env, capture_output=True, text=True, timeout=60)

    def test_copies_files_and_changes_nothing_else(self):
        r = self.install(); self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("nothing is enabled before that", r.stdout)
        for f in ("csb-tui.py", "csb_paths.py", "csb_setup.py", "csb_wizard.py", "csb-hook.py", "hook.ps1", "dashboard.html", "assets/rules.md", "assets/task-intake/SKILL.md", "assets/ct", "assets/cloud/setup-template.sh"):
            self.assertTrue(os.path.isfile(os.path.join(self.board, f)), f)
        self.assertTrue(os.access(os.path.join(self.home, ".local", "bin", "csb"), os.X_OK))
        self.assertEqual(rd(os.path.join(self.home, ".claude", "settings.json")), '{"model": "opus"}')
        self.assertEqual(rd(os.path.join(self.home, ".claude", "CLAUDE.md")), "mine\n")
        for f in ("ntfy.json", "todoist.json", "setup.json"): self.assertFalse(os.path.exists(os.path.join(self.board, f)), f)
        self.assertEqual(sorted(os.listdir(os.path.join(self.home, ".claude"))), ["CLAUDE.md", "settings.json"])
    def test_launcher_runs_the_installed_board(self):
        self.install()
        r = subprocess.run([os.path.join(self.home, ".local", "bin", "csb"), "--uninstall"], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("nothing needed changing", r.stdout)
    def test_installing_twice_is_fine(self):
        self.install(); self.assertEqual(self.install().returncode, 0)
    def test_installed_copy_applies_everything_it_ships(self):
        self.install()
        code = ("import sys; sys.path.insert(0, %r); import csb_setup as s; print(*s.apply({i: True for i in s.ALL_IDS}), sep='\\n')" % self.board)
        r = subprocess.run([sys.executable, "-c", code], env=self.env, capture_output=True, text=True, cwd=self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("rules block written", r.stdout); self.assertIn("skill: task-intake installed", r.stdout); self.assertIn("ct: installed", r.stdout)
        self.assertTrue(os.path.isfile(os.path.join(self.board, "cloud", "setup-script.sh")))
        script = rd(os.path.join(self.board, "cloud", "setup-script.sh"))
        self.assertNotIn("{{", script); self.assertNotIn("__BOARD_TOPIC__", script)
        r = subprocess.run([sys.executable, os.path.join(self.board, "csb_setup.py"), "--uninstall"], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(rd(os.path.join(self.home, ".claude", "CLAUDE.md")), "mine\n"); self.assertEqual(json.loads(rd(os.path.join(self.home, ".claude", "settings.json"))), {"model": "opus"})

if __name__ == "__main__":
    unittest.main()
