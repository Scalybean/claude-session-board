"""bin/ct's first message and lanes, without starting Claude: a throwaway CSB_DIR and repo."""
import importlib.machinery, importlib.util, os, shutil, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def load_ct():
    loader = importlib.machinery.SourceFileLoader("ct", os.path.join(ROOT, "bin", "ct"))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("ct", loader)); loader.exec_module(m)
    return m

class Ct(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); self.old = os.environ.get("CSB_DIR"); os.environ["CSB_DIR"] = self.tmp
        self.ct = load_ct()
    def tearDown(self):
        if self.old is None: os.environ.pop("CSB_DIR", None)
        else: os.environ["CSB_DIR"] = self.old
        shutil.rmtree(self.tmp, True)
    def test_the_first_message_is_a_goal_that_carries_the_task_and_its_lane(self):
        m = self.ct.goal_message("Add a CSV export", "routine", 4100, "task_csv", ["src/routes.ts"])
        self.assertTrue(m.startswith("/goal Task (tier: routine), use the task-intake skill:\nAdd a CSV export"))
        for part in ("port 4100", "task_csv", "src/routes.ts", "needs-human", "never edit a test to match", "how to try it"):
            self.assertIn(part, m)
        self.assertNotIn("task-intake", self.ct.goal_message("fix typo", "quick", 4100, "db", []))
    def test_a_long_task_is_cut_to_the_goal_limit(self):
        m = self.ct.goal_message("x" * 9000, "risky", 4100, "db", [])
        self.assertLessEqual(len(m), self.ct.GOAL_MAX); self.assertIn("[...cut", m)
    def test_ports_dont_repeat(self):
        ports = {self.ct.take_port(f"t{i}") for i in range(5)}
        self.assertEqual(len(ports), 5); self.assertTrue(all(4100 <= p < 4900 for p in ports))
    def test_launches_at_the_same_moment_get_different_ports(self):
        import subprocess, sys
        code = ("import importlib.machinery, importlib.util; l = importlib.machinery.SourceFileLoader('ct', %r); "
                "m = importlib.util.module_from_spec(importlib.util.spec_from_loader('ct', l)); l.exec_module(m); print(m.take_port('t'))") % os.path.join(ROOT, "bin", "ct")
        procs = [subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=dict(os.environ)) for _ in range(8)]
        ports = [int(p.communicate()[0]) for p in procs]
        self.assertEqual(len(set(ports)), 8, ports)
    def test_shared_files_list(self):
        repo = os.path.join(self.tmp, "r"); os.makedirs(os.path.join(repo, "tasks"))
        with open(os.path.join(repo, "tasks", "shared-files.txt"), "w") as f: f.write("# shared\nsrc/routes.ts\n\nconfig/*.json\n")
        self.assertEqual(self.ct.shared_files(repo), ["src/routes.ts", "config/*.json"])
        self.assertEqual(self.ct.shared_files(self.tmp), [])

if __name__ == "__main__":
    unittest.main()
