---
name: task-intake
description: Use when the user hands over a task to run independently or in parallel with other Claude sessions ("run this in the background", "take this task", "work on X on your own"), or starts/maintains an app across sessions. Builds a task packet (goal, acceptance criteria, definition of done, context, review triggers), works in isolation, verifies, hands off cleanly between sessions, and escalates to the human only at agreed triggers.
---

# task-intake

Goal: high-quality autonomous work, human attention only where it matters, and context that survives between sessions.

## Where knowledge lives

| What | Where | Loaded |
|---|---|---|
| Rules that apply to every task | `~/.claude/CLAUDE.md` (keep tiny) | always |
| Cross-repo know-how (preferences, reusable patterns, tool quirks) | `~/.claude/knowledge/` with an `INDEX.md` | on demand via index |
| Repo-specific rules (commands, conventions, hard constraints) | `<repo>/CLAUDE.md`, under ~300 lines | always in that repo |
| Repo knowledge (architecture, decisions + why, gotchas) | `<repo>/docs/` with `docs/INDEX.md` | on demand via index |
| Task packet | `<repo>/tasks/<slug>.md` | per task |
| Feature list + progress log | `<repo>/tasks/FEATURES.json`, `tasks/PROGRESS.md` | read at session start |

**Default: save to the repo.** If you learn something that looks reusable across repos, ask the user in the intake round whether to also save it to `~/.claude/knowledge/` (one option in the batched question, never a separate interruption). Update an existing note instead of adding a duplicate. Put only durable facts in CLAUDE.md: commands, conventions, constraints, decisions. No per-task history, no restated file trees.

## 0. Orient (every session start)

1. Read `<repo>/CLAUDE.md`, `docs/INDEX.md`, `tasks/PROGRESS.md` (last entry) and `git log --oneline -15`. Open other docs only when the index says they matter to this task.
2. Run the baseline check from CLAUDE.md (build/tests). If it is already red, say so and make fixing it the first step or a review trigger; do not build on a broken base.
3. If the repo has none of this, bootstrap it (below).

### Bootstrap - new app
Ask for the idea and constraints, write `docs/spec.md` (users, outcomes, non-goals), then `tasks/FEATURES.json`: a list of end-to-end features, each `{id, title, acceptance, passes:false}`. Create `CLAUDE.md` (commands, conventions), `docs/INDEX.md`, `docs/decisions/` (one short file per decision: context, choice, why, alternatives), an init/run script, and commit small. Present spec + feature list for the human to approve before building.

### Bootstrap - existing project
Audit before changing anything: build and test commands that actually work, structure, entry points, conventions seen in the code, known fragile areas. Write what you verified to `CLAUDE.md` and `docs/` (facts only, mark guesses as unverified). Seed `FEATURES.json` with the behaviour that must not break. Show the user what you found and what you could not verify.

## 1. Size the task first

Pick a tier. When the first prompt has a `tier:` line (from the `ct` launcher) keep it; you may raise it to risky, never lower it. Without one, default to routine; use quick only when the user said so or the change is truly one sentence:

| Tier | What it is | Packet | Review |
|---|---|---|---|
| `quick` | describable as one sentence, one file or a small diff | none; just a step list | self-check + run tests |
| `routine` | normal feature/fix, reversible | short packet | independent reviewer (step 4) |
| `risky` | data, auth, money, prod, schema, deletions, shared infra, or unclear scope | full packet, human approves it before work | independent reviewer + human review before merge |

Do not over-specify small work: no packet, no 16 acceptance criteria for a one-line bug.

## 2. Task packet (routine and risky only)

Ask ONE batched AskUserQuestion round (max 4 questions), tailored to this task; skip what is already known:

1. **Ambiguities that would block or derail the work** - the real forks in this task, not generic questions.
2. **Acceptance criteria** - propose observable outcomes ("a non-member cannot export", not "export works") and let the user correct them.
3. **Human-review triggers** - propose task-specific options (multiSelect): before merge/deploy/send, destructive or irreversible actions, spending money/quota, external systems or other people, prod data, schema or dependency changes, low confidence, same check failing twice, scope growing past the spec.
4. **Autonomy** - approve the plan first, or go straight to execution. Plus, only if relevant: whether any new knowledge should also go to the central folder.

Then stop asking. Write `tasks/<slug>.md`:

```
# <title>
status: planned | running | needs-human | review | done
goal: one or two sentences
tier: routine | risky
acceptance: observable outcomes, one per line, each with `check:` = the command or observation that proves it
definition-of-done: checklist - tests pass, each acceptance line has evidence, no regressions, docs/decisions updated, anything a human must still check
context: pointers to files/docs/decisions that matter (paths, not pasted content)
constraints: must / must-not
review-triggers: exactly what the user chose
decisions: assumptions made, appended as you go
log: one line per milestone
```

**Always create the step list (TaskCreate or TodoWrite) before starting work, including in `claude -p` runs, and keep statuses current.** If no task tool is available, keep the checklist file the plan-nudge hook names (`session-board/plans/<session_id>.md`: `- [ ]`, `- [>]`, `- [x]`). Steps are outcomes in plain words ("Merge PR #15 into main"), never shell commands. The session dashboard's progress bar is computed from this list; without one it shows "no plan". Keep `tasks/FEATURES.json` as a list of `{id, title, acceptance, passes}` since the dashboard shows features passing/total from it.

Prefer acceptance checks that are runnable commands (tests, curl, a script); they become the pass/fail gate. For a long unattended run, set `/goal` to "all acceptance checks in tasks/<slug>.md pass" so an independent judge decides when it is done.

For risky tasks, the human approves the packet (especially acceptance) before execution. Create native tasks (TaskCreate) for the steps with `addBlockedBy` ordering, one in_progress at a time.

## 3. Execute in isolation

- Code: background sessions (`claude --bg`, `ct new`) already get their own worktree; otherwise use `claude --worktree <slug>` or `isolation: worktree` for subagents. Never the main branch directly. Commit after each step, and end every session in a mergeable state.
- For unclear tasks, a cheap scout attempt first is fine: try, learn where the hard parts are, then throw it away and write the packet.
- One feature or task at a time; do not start the next until the current one is verified.
- Non-code outputs go under `tasks/<slug>/`.
- Stay in scope. Out-of-scope discoveries go in the packet's `decisions`/log or as a follow-up note, not into the diff.
- Compact between tasks, not mid-task. Anything that must survive goes in a file first.

## 4. Verify (a gate, not a self-report)

1. Run every `check:` and the definition of done. Paste the real output (trimmed) into the packet as evidence; never claim a pass you did not run.
2. Never delete, skip or loosen tests to get green. If a test is wrong, stop and make it a review trigger. (A Stop hook flags new skips and deleted tests.)
3. **Independent review** (routine and risky): spawn a fresh reviewer that sees only the diff (`git diff <base>...HEAD`) and the packet's goal + acceptance, not this conversation. Use `/code-review`, or a subagent with this brief: "Review this diff against these acceptance criteria. Flag only problems that break correctness or a stated requirement; no style or nice-to-haves." Fix what it finds, or record why not.
4. Record the result in the packet as two lines, `reviewer: pass | fixed: <what> | open: <what>` and `evidence: <checks run and result>`. A Stop hook (review gate) blocks marking a routine/risky packet `review`/`done` without them.
5. Mark a feature `passes:true` in `FEATURES.json` only after this. Two failures of the same check is a review trigger.

## 5. Escalate

When a trigger fires or you are blocked:

1. Set `status: needs-human` with the exact question and your recommended answer.
2. Ask in-session (AskUserQuestion or end the turn with the question). The dashboard shows it as blocked, raises a desktop toast, and pushes to the phone via ntfy if it waits (`ntfy.json` in the session-board folder). Do not duplicate that.
3. Use Todoist only if the user explicitly asks for a durable to-decide list: project `Claude Review`, one task per session, label `claude`, no subtasks, due date only when a ping is wanted, complete it once the user decides, no progress updates.
4. Keep working on unblocked parts meanwhile.

## 6. Hand off (every session end)

**Code tasks end with a pull request** (routine and risky): work on the task's own branch (background sessions already have one), push that branch, and open a PR with `gh pr create`: title = the outcome, body = goal, acceptance with evidence, reviewer verdict, what's unverified, and the packet path. Never push task work straight to main/master; a push guard asks the user first if you try. The dashboard shows the PR's state and checks.

Append to `tasks/PROGRESS.md` (the session record; there is no activity log anymore). Keep it short: when it has more than 10 entries, fold the oldest into a few lines under `## Earlier` at the top. date, tier, what was done (specific, outcome-first, since this feeds the notification text), evidence (checks run + result, reviewer verdict), decisions and why, what is next, what is unverified or risky, diff size. Update `docs/` and `docs/decisions/` for anything durable; keep CLAUDE.md for rules only. Set the packet to `done` or leave it `running` with a clear next step. Set packet `status: review` when it waits for the human. Final reply: 3-5 lines on what was produced, where it is, and what still needs the human.
