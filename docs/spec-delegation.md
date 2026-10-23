# Delegation and review

One loop between you and Claude: a task can be with you or with Claude, and moves between you.

```
 your task (Todoist)
      │  c  give to Claude
      ▼
 delegation form ── context: task notes + comments, files/folders, links/docs, an earlier session or PR
      │             definition of done: what proves it's finished
      │             where: a repo (code) or ~/claude-work (research, writing, admin) · local or cloud
      ▼
 With Claude ────── an ordinary background session; its questions still show as "needs you"
      ▼
 finished ───────── no testing asked: the task shows "done by Claude" and you check it off when you're happy
      │             testing asked ("will you test it yourself?" in the form):
      ▼
 Test: <task> ───── appears in your Today list with what you said you'd check, Claude's summary, PR or result
      ├─ approve   code: merge the PR once checks pass, then complete the Todoist task · other: complete it
      ├─ send back with a note → the same session continues
      └─ reject    → session stopped, the task stays yours
```

## Decisions
- **Board only.** Delegation and review state live in `session-board/delegations.json`. Todoist holds only your own
  tasks; the board draws `→ Claude` / `for review` badges next to them. Nothing AI-related is written to Todoist
  except completing a task you approved.
- **Review means you test it yourself, and only when you say so.** The form asks "will you test it yourself?"; what
  you type is the checklist. Without it, finished work is just a finished session plus a "done by Claude" badge on the
  task. Plain `ct` / `n` launches are ordinary sessions; `ct --review "what to check"` opts one in.
- **Test items live in Today** (board only), under "To test". The sessions header and `ct`'s limit count these.
- **Kinds of work:** code in a repo (worktree + PR), research/writing, admin with connected tools, cloud runs.
- **Non-code work** runs in `~/claude-work`, a git folder used only for this (background sessions need a git repo).
  Each task gets its own folder there for its result. Admin tasks never send anything without your approval.
- **Cloud runs:** Claude Code only starts new cloud sessions interactively, so the board opens a terminal with the
  command typed in and you press Enter. The cloud reporter then shows it on the board as usual.
- **Approve merges** code PRs (squash) only when checks pass; otherwise it says why and waits.

## Phases (1 is built)
1. Claude tab; delegate code and research tasks; approve / send back / reject; review items for `ct` launches.
2. Context attachments (files, links, Drive docs, earlier sessions); admin tasks with connectors in `~/claude-work`.
3. Cloud runs.

## State of an item
`with_claude` → `done` (no testing asked) or `for_review` (you asked to test) → `approved` | `rejected`;
`send back` returns it to `with_claude`. A question asked with a question prompt or a permission request keeps it `with_claude` ("needs you");
a question written as plain text at the end of a turn moves it to `for_review`, where "send back" is the answer.
