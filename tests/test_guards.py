"""The hooks that run in every session: start hook, test guard, review gate, push guard, plan nudge, status line, and the
board's pruning. Each runs as Claude Code runs it (JSON on stdin) against throwaway git repos and a throwaway CSB_DIR."""
import json, os, shutil, subprocess, sys, tempfile, time, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOARD = os.path.join(ROOT, "board")
sys.path.insert(0, BOARD)

def sh(cwd, *cmd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True).stdout

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); self.board = os.path.join(self.tmp, "board")
        os.makedirs(os.path.join(self.board, "sessions"))
        self.env = dict(os.environ, CSB_DIR=self.board, TMPDIR=self.tmp, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t", HOME=self.tmp)
        self.repo = self.make_repo("repo")
    def tearDown(self):
        shutil.rmtree(self.tmp, True)
    def make_repo(self, name):
        r = os.path.join(self.tmp, name); os.makedirs(os.path.join(r, "tests"))
        sh(r, "git", "init", "-q", "-b", "main")
        sh(r, "git", "config", "user.email", "t@t"); sh(r, "git", "config", "user.name", "t")
        self.write(r, "tests/test_a.py", "def test_a():\n    pass\n\ndef test_b():\n    pass\n")
        self.write(r, "README.md", "hello\n")
        sh(r, "git", "add", "-A"); sh(r, "git", "commit", "-qm", "init")
        return r
    def write(self, repo, path, text):
        p = os.path.join(repo, path); os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f: f.write(text)
    def run_hook(self, script, ev, env=None):
        r = subprocess.run([sys.executable, os.path.join(BOARD, script)], input=json.dumps(ev), capture_output=True, text=True,
                           env=env or self.env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr); self.assertEqual(r.stderr, "")
        return json.loads(r.stdout) if r.stdout.strip() else None
    def start(self, sid="s1", cwd=None, event="UserPromptSubmit"):
        return self.run_hook("csb-start.py", {"session_id": sid, "hook_event_name": event, "cwd": cwd or self.repo, "source": "resume" if event == "SessionStart" else "startup", "prompt": "go"})
    def stop(self, script, sid="s1", cwd=None, **kw):
        return self.run_hook(script, dict({"session_id": sid, "hook_event_name": "Stop", "cwd": cwd or self.repo}, **kw))

class FailOpen(Base):
    def test_a_missing_script_never_blocks(self):
        import csb_setup
        env = dict(self.env, CSB_DIR=os.path.join(self.tmp, "gone"))
        os.environ["CSB_DIR"] = env["CSB_DIR"]
        try: cmd = csb_setup.py("csb-push-guard.py")
        finally: os.environ.pop("CSB_DIR")
        r = subprocess.run(["sh", "-c", cmd], input="{}", capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0)          # plain `python3 missing.py` exits 2, which Claude Code reads as "block"

class TestGuard(Base):
    G = "csb-test-guard.py"
    def test_older_approved_deletion_on_the_branch_does_not_block_a_new_session(self):
        sh(self.repo, "git", "checkout", "-qb", "feat"); os.remove(os.path.join(self.repo, "tests/test_a.py"))
        sh(self.repo, "git", "commit", "-qam", "approved removal")
        for sid in ("A", "B"):
            self.start(sid); self.assertIsNone(self.stop(self.G, sid))
    def test_this_sessions_deletion_blocks_once(self):
        self.start(); os.remove(os.path.join(self.repo, "tests/test_a.py"))
        out = self.stop(self.G); self.assertEqual(out["decision"], "block"); self.assertIn("2 test case(s) removed", out["reason"])
        self.assertIsNone(self.stop(self.G))                                   # same finding: told once
        self.assertIsNone(self.stop(self.G, sid="s2", stop_hook_active=True))
    def test_a_committed_deletion_in_this_session_counts(self):
        self.start(); os.remove(os.path.join(self.repo, "tests/test_a.py")); sh(self.repo, "git", "commit", "-qam", "rm")
        self.assertEqual(self.stop(self.G)["decision"], "block")
    def test_skip_mentioned_outside_tests_is_fine(self):
        self.start()
        self.write(self.repo, "README.md", "Use @pytest.mark.skip sparingly\n"); self.write(self.repo, "lib.js", "x = list.only(1)\n")
        self.assertIsNone(self.stop(self.G))
    def test_skip_with_a_reason_is_fine_bare_skip_blocks(self):
        self.start()
        self.write(self.repo, "tests/test_c.py", 'import pytest, sys\n@pytest.mark.skipif(sys.platform == "win32", reason="posix only")\ndef test_c():\n    pass\n')
        self.assertIsNone(self.stop(self.G))
        self.write(self.repo, "tests/test_d.py", "import pytest\n@pytest.mark.skip\ndef test_d():\n    pass\n")
        out = self.stop(self.G); self.assertIn("without a reason", out["reason"]); self.assertIn("test_d.py", out["reason"])
    def test_skip_markers_inside_strings_are_fixtures_not_skips(self):
        self.start()
        self.write(self.repo, "tests/test_fixture.py", 'def test_f():\n    write("x.py", "@pytest.mark.skip\\ndef t(): pass")\n    write("a.js", "it.only(1)")\n')
        self.assertIsNone(self.stop(self.G))
    def test_other_peoples_commits_brought_in_by_a_merge_or_checkout_dont_count(self):
        sh(self.repo, "git", "checkout", "-qb", "feat"); sh(self.repo, "git", "checkout", "-q", "main")
        os.remove(os.path.join(self.repo, "tests/test_a.py"))
        old = dict(self.env, GIT_COMMITTER_DATE="2026-01-01T00:00:00", GIT_AUTHOR_DATE="2026-01-01T00:00:00")
        subprocess.run(["git", "commit", "-qam", "teammate removes a test"], cwd=self.repo, env=old, check=True)
        sh(self.repo, "git", "checkout", "-q", "feat")
        self.start(); sh(self.repo, "git", "merge", "-q", "main")
        self.assertIsNone(self.stop(self.G))
        self.start("s2"); sh(self.repo, "git", "checkout", "-q", "-b", "other", "main")
        self.assertIsNone(self.stop(self.G, "s2"))
    def test_method_names_that_look_like_skips_are_fine(self):
        self.start(); self.write(self.repo, "tests/test_a.py", "def test_a():\n    m.fit(X, y)\n    qs.only(1)\n\ndef test_b():\n    pass\n")
        self.assertIsNone(self.stop(self.G))
    def test_a_repo_entered_mid_turn_without_a_start_record_is_not_blamed(self):
        other = self.make_repo("other"); os.remove(os.path.join(other, "tests/test_a.py"))     # someone else's work
        self.start(); self.assertIsNone(self.stop(self.G, cwd=other))
    def test_a_resume_keeps_the_start_so_work_before_an_interruption_counts(self):
        self.start(); os.remove(os.path.join(self.repo, "tests/test_a.py"))
        self.start(event="SessionStart")                                  # source=resume in start(); interrupted, then resumed
        self.assertEqual(self.stop(self.G)["decision"], "block")
    def test_out_of_time_means_quiet(self):
        import csb_session as cs
        cs.budget(0)
        try: self.assertEqual(cs.git(self.repo, "status"), "")
        finally: cs._deadline = None
    def test_js_only_blocks(self):
        self.start(); self.write(self.repo, "tests/a.test.js", "it.only('x', () => {})\n")
        self.assertEqual(self.stop(self.G)["decision"], "block")
    def test_a_test_moved_to_a_new_untracked_file_balances(self):
        self.start()
        self.write(self.repo, "tests/test_a.py", "def test_a():\n    pass\n")
        self.write(self.repo, "tests/test_moved.py", "def test_b():\n    pass\n")
        self.assertIsNone(self.stop(self.G))
    def test_changes_already_there_at_the_start_are_not_this_sessions(self):
        os.remove(os.path.join(self.repo, "tests/test_a.py"))          # someone else's uncommitted work
        self.start(); self.write(self.repo, "README.md", "edited\n")
        self.assertIsNone(self.stop(self.G))
    def test_outside_git_and_subagents_are_quiet(self):
        self.assertIsNone(self.stop(self.G, cwd=self.tmp)); self.assertIsNone(self.stop(self.G, agent_id="x"))

class ReviewGate(Base):
    G = "csb-review-gate.py"
    PACKET = "tier: risky\nstatus: done\ngoal: x\n"
    def test_untouched_old_packet_does_not_block(self):
        self.write(self.repo, "tasks/old.md", self.PACKET); sh(self.repo, "git", "add", "-A"); sh(self.repo, "git", "commit", "-qm", "p")
        for sid in ("rg-2", "rg-3"):
            self.start(sid); self.assertIsNone(self.stop(self.G, sid))
    def test_marking_done_without_review_blocks(self):
        self.start(); self.write(self.repo, "tasks/new.md", self.PACKET)
        out = self.stop(self.G); self.assertEqual(out["decision"], "block"); self.assertIn("no reviewer / no evidence", out["reason"])
        self.write(self.repo, "tasks/new.md", self.PACKET + "reviewer: pass\nevidence: tests pass\n")
        self.assertIsNone(self.stop(self.G))
    def test_fields_inside_a_code_block_dont_count(self):
        self.start(); self.write(self.repo, "tasks/template.md", "# Template\n\n```\n" + self.PACKET + "```\n")
        self.assertIsNone(self.stop(self.G))

class PushGuard(Base):
    def ask(self, cmd, cwd=None):
        out = self.run_hook("csb-push-guard.py", {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": cwd or self.repo})
        return bool(out and out["hookSpecificOutput"]["permissionDecision"] == "ask")
    def test_cases(self):
        feat = self.make_repo("feat"); sh(feat, "git", "checkout", "-qb", "feature")
        asks = ["git push", "git push origin main", "cd . && git push", "git push origin feat:main", "git push origin HEAD",
                "git push origin refs/heads/x:refs/heads/main", "git push --all", "FOO=1 git push"]
        quiet = ['echo "remember to git push later"', "git push --tags", "git push origin --delete old",
                 'git commit -m "stop calling git push in the script"', 'git commit -m "a\ngit push origin main"',
                 "git push origin v1.0:refs/tags/v1.0", "git pushx"]
        for c in asks: self.assertTrue(self.ask(c), c)
        for c in quiet: self.assertFalse(self.ask(c), c)
        self.assertFalse(self.ask("git push", feat))
        self.assertTrue(self.ask(f"git -C {self.repo} push", feat))      # -C: the other repo's branch is what's pushed
        self.assertTrue(self.ask(f"cd {self.repo}\ngit push", feat))
        for c in ["git push --tags origin main", "git push origin main --tags", "git push origin main -d feat2", "git push --signed origin main",
                  "(cd . && git push origin main)", "sudo git push origin main", "sudo -u me git push origin main", "command git push origin main",
                  "timeout 60 git push origin main", "env A=1 git push origin main", 'bash -c "git push origin main"',
                  "git push origin 'refs/heads/*:refs/heads/*'", "git push origin --delete main", "git push origin :main"]:
            self.assertTrue(self.ask(c, feat), c)
        for c in ["git push -d origin feat2", "git push origin --tags", 'bash -c "echo git push origin main"', "git push -n origin main"]:
            self.assertFalse(self.ask(c, feat), c)
    def test_feature_branch_that_tracks_main(self):
        r = self.make_repo("up"); sh(r, "git", "checkout", "-qb", "feature")
        sh(r, "git", "config", "branch.feature.remote", "."); sh(r, "git", "config", "branch.feature.merge", "refs/heads/main")
        self.assertTrue(self.ask("git push", r))

class PlanNudge(Base):
    def nudge(self, n, env=None):
        return self.run_hook("csb-plan-nudge.py", {"session_id": "p1", "hook_event_name": "UserPromptSubmit", "cwd": self.repo, "prompt": f"thanks {n}"}, env)
    def test_one_reminder_then_quiet_while_nothing_runs(self):
        self.assertIsNotNone(self.nudge(1)); self.assertIsNone(self.nudge(2)); self.assertIsNone(self.nudge(3))
    def test_no_board_folder_means_no_output_and_no_folder(self):
        gone = os.path.join(self.tmp, "gone")
        self.assertIsNone(self.nudge(1, dict(self.env, CSB_DIR=gone))); self.assertFalse(os.path.exists(gone))

class SharedFolder(Base):
    def other(self, sid, cwd, state="working", age_s=10):
        with open(os.path.join(self.board, "sessions", sid + ".json"), "w") as f:
            json.dump({"id": sid, "cwd": cwd, "state": state, "name": "Other task", "lastEventAt": int((time.time() - age_s) * 1000)}, f)
    def test_warns_once_about_a_live_session_in_the_same_tree(self):
        self.other("o1", os.path.join(self.repo, "tests"))
        out = self.start(); msg = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("same folder", msg); self.assertIn("Other task", msg)
        self.assertIsNone(self.start())
    def test_ended_stale_or_elsewhere_sessions_are_ignored(self):
        self.other("o1", self.repo, state="ended"); self.other("o2", self.repo, age_s=13 * 3600)
        self.other("o3", self.make_repo("elsewhere"))
        self.assertIsNone(self.start())
    def test_another_worktree_is_fine(self):
        wt = os.path.join(self.tmp, "wt"); sh(self.repo, "git", "worktree", "add", "-q", "-b", "side", wt)
        self.other("o1", wt); self.assertIsNone(self.start())

class Prune(Base):
    def test_prune(self):
        import csb_prune
        b = self.board; old = time.time() - 30 * 86400
        for p in ("plans/.state/a.json", "plans/.state/b.json", "baseline/a.json", "usage.json.1.tmp"):
            self.write(b, p, "{}")
        os.utime(os.path.join(b, "plans/.state/a.json"), (old, old)); os.utime(os.path.join(b, "baseline/a.json"), (old, old))
        os.utime(os.path.join(b, "usage.json.1.tmp"), (old, old))
        for i in range(5): os.makedirs(os.path.join(b, f"backup-2026101{i}-000000"))
        self.write(b, "hook.ps1.bak-20261009-000000", "x"); self.write(b, "plans/keep.md", "- [ ] x\n")
        for m in ("2026-01", "2026-04", "2026-10"): self.write(b, f"history/{m}.jsonl", "")
        now = time.mktime((2026, 10, 23, 12, 0, 0, 0, 0, -1))
        gone = csb_prune.prune(b, now=now)
        left = lambda d: sorted(os.listdir(os.path.join(b, d)))
        self.assertEqual(left("plans/.state"), ["b.json"]); self.assertEqual(left("baseline"), [])
        self.assertEqual(sorted(x for x in os.listdir(b) if x.startswith(("backup-", "hook"))),
                         ["backup-20261012-000000", "backup-20261013-000000", "backup-20261014-000000"])
        self.assertEqual(left("history"), ["2026-04.jsonl", "2026-10.jsonl"])
        self.assertTrue(os.path.exists(os.path.join(b, "plans/keep.md"))); self.assertFalse(os.path.exists(os.path.join(b, "usage.json.1.tmp")))
        self.assertTrue(gone); self.assertEqual(csb_prune.prune(b, now=now), [])         # once a day

class StatusLine(Base):
    def test_no_temp_file_left(self):
        ev = {"session_id": "x", "model": {"display_name": "M"}, "rate_limits": {"five_hour": {"used_percentage": 5}}}
        r = subprocess.run([sys.executable, os.path.join(BOARD, "csb-statusline.py")], input=json.dumps(ev), capture_output=True, text=True, env=self.env)
        self.assertIn("5h 5%", r.stdout)
        self.assertEqual([f for f in os.listdir(self.board) if f.endswith(".tmp")], [])
        self.assertTrue(os.path.exists(os.path.join(self.board, "usage.json")))

if __name__ == "__main__":
    unittest.main()
