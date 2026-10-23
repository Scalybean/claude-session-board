"""board/csb_setup.py in a throwaway HOME and session-board folder. Never touches the real ~/.claude."""
import json, os, shutil, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "board"))
import csb_setup as su

USER_SETTINGS = {"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}],
                                          "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "my-linter"}]}]}}
USER_MD = "# My own rules\n\n- be nice\n"

def rd(p, mode="r"):
    with open(p, mode) as f: return f.read()

def wr(p, text):
    with open(p, "w") as f: f.write(text)

def jload(p):
    with open(p) as f: return json.load(f)

def tree(root):
    out = {}
    for d, _, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f); out[os.path.relpath(p, root)] = rd(p, "rb")
    return out

class Setup(unittest.TestCase):
    def setUp(self):
        self.env = dict(os.environ)
        self.home = tempfile.mkdtemp(); self.board = os.path.join(self.home, "board")
        os.environ.update(HOME=self.home, CSB_DIR=self.board); os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.makedirs(os.path.join(self.home, ".claude"))
        self.settings = os.path.join(self.home, ".claude", "settings.json"); self.md = os.path.join(self.home, ".claude", "CLAUDE.md")
        wr(self.settings, json.dumps(USER_SETTINGS, indent=4)); wr(self.md, USER_MD)
        self.settings_bytes = rd(self.settings, "rb")
    def tearDown(self):
        os.environ.clear(); os.environ.update(self.env); shutil.rmtree(self.home, True)
    def hooks(self):
        d = jload(self.settings); return d, [h["command"] for g in sum(d.get("hooks", {}).values(), []) for h in g["hooks"]]
    ALL = {i: True for i in su.ALL_IDS}

    def test_all_no_changes_nothing(self):
        su.apply({});
        self.assertEqual(rd(self.settings, "rb"), self.settings_bytes); self.assertEqual(rd(self.md), USER_MD)
        for f in ("ntfy.json", "todoist.json", "ct.json"): self.assertFalse(os.path.exists(os.path.join(self.board, f)), f)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local")))
        self.assertEqual([f for f in os.listdir(os.path.join(self.home, ".claude")) if "bak" in f], [])
    def test_all_yes_adds_exactly_the_board_entries_and_keeps_the_users(self):
        log = su.apply(self.ALL, {"todoist_token": "tok", "ct_max_running": 3})
        d, cmds = self.hooks()
        self.assertEqual(d["model"], "opus"); self.assertIn("echo mine", cmds); self.assertIn("my-linter", cmds)
        ours = [c for c in cmds if su.is_ours(c)]
        for script in ("csb-plan-nudge", "csb-push-guard", "csb-test-guard", "csb-review-gate", "csb-history", "csb-hook"):
            self.assertTrue(any(script in c for c in ours), script)
        self.assertEqual(len(cmds), len(ours) + 2)
        self.assertIn("statusLine", d)
        md = rd(self.md)
        self.assertTrue(md.startswith(USER_MD)); self.assertEqual(md.count(su.MARK_START), 1); self.assertNotIn("graphify", md)
        self.assertTrue(os.path.isfile(os.path.join(self.home, ".claude", "skills", "task-intake", "SKILL.md")))
        self.assertTrue(os.path.isfile(os.path.join(self.home, ".claude", "agents", "task-reviewer.md")))
        self.assertTrue(os.access(os.path.join(self.home, ".local", "bin", "ct"), os.X_OK))
        self.assertEqual(jload(os.path.join(self.board, "ct.json"))["max_running"], 3)
        self.assertEqual(jload(os.path.join(self.board, "todoist.json")), {"token": "tok"})
        ntfy = jload(os.path.join(self.board, "ntfy.json"))
        self.assertTrue(ntfy["enabled"] and ntfy["topic"].startswith("claude-") and ntfy["board_topic"].startswith("claude-board-"))
        self.assertTrue(os.path.isfile(os.path.join(self.board, "cloud", "setup-script.sh")))
        self.assertTrue([l for l in log if l.startswith("backup:")])
        self.assertTrue([f for f in os.listdir(os.path.join(self.home, ".claude")) if f.startswith("settings.json.bak-claude-tasks")])
    def test_second_run_changes_nothing(self):
        su.apply(self.ALL); before = tree(self.home)
        su.apply(self.ALL); after = tree(self.home)
        self.assertEqual({k: v for k, v in before.items() if "board/setup.json" not in k}, {k: v for k, v in after.items() if "board/setup.json" not in k})
    def test_uninstall_restores_the_users_files(self):
        su.apply(self.ALL); su.uninstall()
        self.assertEqual(jload(self.settings), USER_SETTINGS); self.assertEqual(rd(self.md), USER_MD)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "skills", "task-intake", "SKILL.md")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".local", "bin", "ct")))
        self.assertFalse(os.path.exists(os.path.join(self.board, "todoist.json")))
        self.assertFalse(os.path.exists(os.path.join(self.board, "ntfy.json")))      # setup created it, so uninstall removes it (no file = pushes off)
    def test_unticking_one_guard_removes_only_it(self):
        su.apply(self.ALL); su.apply(dict(self.ALL, g_push=False))
        _, cmds = self.hooks()
        self.assertFalse(any("csb-push-guard" in c for c in cmds)); self.assertTrue(any("csb-test-guard" in c for c in cmds)); self.assertIn("my-linter", cmds)
    def test_only_ticked_rules_are_written(self):
        su.apply({"tests": True}); md = rd(self.md)
        self.assertIn("Never weaken, skip or delete tests", md); self.assertNotIn("Working style", md); self.assertNotIn("task-intake", md)
    def test_an_existing_status_line_stays(self):
        d = dict(USER_SETTINGS, statusLine={"type": "command", "command": "my-status"}); wr(self.settings, json.dumps(d))
        su.apply(self.ALL); self.assertEqual(jload(self.settings)["statusLine"]["command"], "my-status")
        su.uninstall(); self.assertEqual(jload(self.settings)["statusLine"]["command"], "my-status")
    def test_invalid_settings_json_is_left_alone(self):
        wr(self.settings, "{ not json")
        with self.assertRaises(ValueError): su.apply(self.ALL)
        self.assertEqual(rd(self.settings), "{ not json")
    def test_no_settings_file_yet(self):
        os.remove(self.settings); su.apply({"track": True}); d, cmds = self.hooks(); self.assertTrue(all(su.is_ours(c) for c in cmds))
    def test_cloud_needs_ntfy(self):
        su.apply({"cloud": True}); self.assertFalse(os.path.exists(os.path.join(self.board, "cloud", "setup-script.sh")))
    def test_needs_setup_until_answered(self):
        self.assertTrue(su.needs_setup()); su.apply({}); self.assertFalse(su.needs_setup())

    # ---- regressions from the independent review ----
    def test_a_hook_wired_by_hand_with_the_identical_command_survives_uninstall(self):
        cmd = su.hook_specs({"g_push": True})[0]["command"]
        d = dict(USER_SETTINGS); d["hooks"] = dict(d["hooks"], PreToolUse=[{"matcher": "Bash|PowerShell", "hooks": [{"type": "command", "command": cmd, "timeout": 10}]}])
        wr(self.settings, json.dumps(d))
        su.apply({"g_push": True}); su.uninstall()
        self.assertIn(cmd, self.hooks()[1])
    def test_claude_md_round_trips_byte_for_byte(self):
        for original in ("A\r\nB\r\n", "A", "A\n\n", "A\n", "﻿A\n"):
            with open(self.md, "w", encoding="utf-8", newline="") as f: f.write(original)
            su.apply({"tests": True}, st={"version": 1, "done": False, "choices": {}, "extras": {}, "applied": {}})
            added = su.read_raw(self.md); self.assertIn(su.MARK_START, added)
            if "\r\n" in original: self.assertNotIn("\n", added.replace("\r\n", ""))      # the block follows the file's line endings
            su.uninstall()
            self.assertEqual(su.read_raw(self.md), original, repr(original))
    def test_a_claude_md_that_setup_created_is_deleted_again(self):
        os.remove(self.md); su.apply({"tests": True}); self.assertTrue(os.path.isfile(self.md)); su.uninstall(); self.assertFalse(os.path.exists(self.md))
    def test_existing_config_files_come_back_on_uninstall(self):
        os.makedirs(self.board); os.makedirs(os.path.join(self.home, ".local", "bin"))
        mine = {"todoist.json": '{"token": "old"}', "ntfy.json": '{"enabled": true, "topic": "my-topic", "board_topic": "my-board"}',
                "ct.json": '{"plugins": {"a": "a@b"}}'}
        for n, t in mine.items(): wr(os.path.join(self.board, n), t)
        wr(os.path.join(self.home, ".local", "bin", "ct"), "#!/bin/sh\necho my ct\n")
        su.apply({"todoist": True, "ntfy": True, "ct": True}, {"todoist_token": "new"}); su.uninstall()
        for n, t in mine.items(): self.assertEqual(rd(os.path.join(self.board, n)), t, n)
        self.assertEqual(rd(os.path.join(self.home, ".local", "bin", "ct")), "#!/bin/sh\necho my ct\n")
    def test_settings_json_with_a_bom_works(self):
        with open(self.settings, "w", encoding="utf-8-sig") as f: json.dump(USER_SETTINGS, f)
        su.apply({"g_push": True}); self.assertTrue(any("csb-push-guard" in c for c in self.hooks()[1]))
    def test_uninstall_on_broken_settings_reports_instead_of_crashing(self):
        su.apply({"g_push": True}); wr(self.settings, "{ broken")
        out = su.uninstall(); self.assertTrue(out[0].startswith("Setup could not finish")); self.assertEqual(rd(self.settings), "{ broken")
    def test_cloud_without_ntfy_is_not_recorded_as_on(self):
        su.apply({"cloud": True}); self.assertFalse(su.load_state()["choices"]["cloud"])

if __name__ == "__main__":
    unittest.main()
