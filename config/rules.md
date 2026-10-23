<!-- rule:style -->
## Working style
- Act when you have enough information; ask only about real forks that change the result. Batch questions into one round.
- Verify before claiming done: run the build/tests or the check that proves it, and say plainly what was not verified.
- Stay in scope. Note out-of-scope findings instead of fixing them.
<!-- rule:tests -->
## Tests
- Never weaken, skip or delete tests, or change an expected value, to make them pass. If tests contradict each other or one looks wrong, say so and ask which behaviour is right.
<!-- rule:steps -->
## Progress tracking (session board)
- For any task with more than two steps, keep a step list with your task tool (TaskCreate/TaskUpdate, or TodoWrite).
- Steps are outcomes in plain words ("Merge PR #15 into main"), never shell commands. One step in progress at a time; mark each done as soon as it is.
<!-- rule:handoff -->
## Multi-session work
- At the start of work in a repo, read its CLAUDE.md, docs/INDEX.md and tasks/PROGRESS.md if they exist.
- Session history goes in the repo's tasks/PROGRESS.md (what was done, decisions and why, next, unverified). Durable knowledge goes in docs/.
<!-- rule:compact -->
## Compact instructions
When compacting, keep: the user's goal and constraints, decisions made and why, files changed, failing checks, and the next step.
<!-- rule:skill -->
## Delegated work
- For tasks run independently, in parallel or across sessions, use the `task-intake` skill.
