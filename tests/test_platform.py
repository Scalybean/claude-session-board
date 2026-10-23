"""Windows-only features are hidden or replaced where they cannot work. The board is loaded with CSB_TABS=0 (no Windows
Terminal / PowerShell, as on macOS and Linux) and with CSB_TABS=1, and its key bar, help and actions are checked."""
import importlib.machinery, importlib.util, os, subprocess, sys, tempfile, unittest, unittest.mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def load_board(tabs):
    os.environ["CSB_TABS"] = tabs; os.environ["CSB_DIR"] = tempfile.mkdtemp()
    sys.path.insert(0, os.path.join(ROOT, "board"))
    loader = importlib.machinery.SourceFileLoader("csb_tui_" + tabs, os.path.join(ROOT, "board", "csb-tui.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader); m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m

def on(platform):
    return unittest.mock.patch.object(sys, "platform", platform)

class Gating(unittest.TestCase):
    def test_without_tabs(self):
        m = load_board("0")
        self.assertFalse(m.TABS)
        self.assertEqual(m.Help.RIGHT[0][1][0], ("a", "attach a background session"))
        self.assertIn(("c", "copy the resume command"), m.Help.RIGHT[0][1])
    def test_with_tabs(self):
        m = load_board("1")
        self.assertTrue(m.TABS)
        self.assertEqual(m.Help.RIGHT[0][1][0], ("a", "go to it, or attach"))
    def test_new_session_copies_the_command_instead_of_opening_a_tab(self):
        m = load_board("0"); said = []
        board = m.App.__new__(m.App); board.say = lambda t, d=0: said.append(t)
        with unittest.mock.patch.object(m, "copy", lambda text, label: label + " copied: " + text), \
             unittest.mock.patch.object(m.subprocess, "run", side_effect=AssertionError("must not start wt/powershell")):
            board.new_session("/work/my app", "--resume abc")
        self.assertIn("cd '/work/my app' && claude --resume abc", said[0])
    def test_jump_and_app_focus_explain_instead_of_calling_powershell(self):
        m = load_board("0"); said = []
        board = m.App.__new__(m.App); board.say = lambda t, d=0: said.append(t)
        with unittest.mock.patch.object(m.subprocess, "Popen", side_effect=AssertionError("no helper may start")), \
             unittest.mock.patch.object(m.subprocess, "run", side_effect=AssertionError("no helper may start")):
            board.jump({"name": "x"}); board.focus_app({"name": "x"})
        self.assertEqual(len(said), 2)
    def test_toast_backends(self):
        m = load_board("0")
        with on("darwin"), unittest.mock.patch.object(m.shutil, "which", lambda n: "/usr/bin/" + n):
            self.assertEqual(m.toast_command("T", "B")[0][0], "osascript")
        with on("linux"), unittest.mock.patch.object(m.shutil, "which", lambda n: "/usr/bin/notify-send" if n == "notify-send" else None):
            self.assertEqual(m.toast_command("T", "B")[0][0], "notify-send")
        with on("linux"), unittest.mock.patch.object(m.shutil, "which", lambda n: None):
            self.assertIsNone(m.toast_command("T", "B"))
        m = load_board("1")
        with on("linux"): self.assertIn("powershell", m.toast_command("T", "B")[0][0])

if __name__ == "__main__":
    unittest.main()
