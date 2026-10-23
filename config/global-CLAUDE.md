# Global instructions

## Working style
- Act when you have enough information; ask only about real forks that change the result. Batch questions into one round.
- Verify before claiming done: run the build/tests or the check that proves it, and say plainly what was not verified.
- Stay in scope. Note out-of-scope findings instead of fixing them.
- Prefer CLIs (`gh`, `vercel`, `supabase`) over MCP tools when both can do the job.
- Never weaken, skip or delete tests to make them pass. If a test is wrong, say so and ask.

## Progress tracking (Claude Sessions dashboard)
- For any task with more than two steps, keep a step list with your task tool (TaskCreate/TaskUpdate, or TodoWrite). Without a task tool, keep the checklist file the session-board hook names.
- Steps are outcomes in plain words ("Merge PR #15 into main"), never shell commands. One step in progress at a time; mark each done as soon as it is.

## Delegated and multi-session work
- For tasks run independently, in parallel or across sessions, use the `task-intake` skill.
- At the start of work in a repo, read its CLAUDE.md, docs/INDEX.md and tasks/PROGRESS.md if they exist.
- Session history goes in the repo's tasks/PROGRESS.md (what was done, decisions and why, next, unverified). Durable knowledge goes in docs/; save it in the repo by default and ask before adding to ~/.claude/knowledge/.

## Compact instructions
When compacting, keep: the user's goal and constraints, decisions made and why, files changed, failing checks, and the next step.

## graphify
When the user types `/graphify`, use the graphify skill before doing anything else.
