# Claude Code surfaces the board could use (2.1.302)

Found 2026-10-18 by reading strings from the installed binary (`~/.local/share/claude/versions/2.1.302`, Bun-compiled, the JS bundle with its Zod hook-input schemas is embedded) and the state files under `~/.claude`. "Schema" = seen in the binary only; "observed" = seen in a real file or command output. Re-check after Claude Code updates.

## Hook events (schema)
Full list: PreToolUse, PostToolUse, PostToolUseFailure, PostToolBatch, Notification, UserPromptSubmit, UserPromptExpansion, SessionStart, SessionEnd, Stop, StopFailure, SubagentStart, SubagentStop, PreCompact, PostCompact, PreModelSwitch, PostModelSwitch, PermissionRequest, PermissionDenied, Setup, TeammateIdle, TaskCreated, TaskCompleted, Elicitation, ElicitationResult, ConfigChange, WorktreeCreate, WorktreeRemove, InstructionsLoaded, CwdChanged, FileChanged, DirectoryAdded, MessageDisplay.

hook.ps1 handles SessionStart, UserPromptSubmit, PostToolUse(+Failure), PermissionRequest, Notification, Stop, StopFailure, SessionEnd. Fields it does not use yet:
- `Stop.background_tasks[]`: `{id, type (shell|subagent|monitor|workflow), status, description, server?, tool?, name?}`. Per the binary, it "lets hooks distinguish 'session is done' from 'session is paused waiting for background work to wake it'". `Stop.session_crons[]`: `{id, schedule, ...}` for CronCreate / ScheduleWakeup / /loop wakeups.
- `StopFailure.error`: one of authentication_failed, oauth_org_not_allowed, account_on_hold, verification_required, billing_error, rate_limit, overloaded, invalid_request, model_not_found, server_error, unknown, max_output_tokens, cloud_credential_error; also `error_details`.
- `SessionStart`: `source` (startup|resume|clear|compact|fork), `session_title`; on resume/fork also `seconds_since_last_response`, `context_tokens`, `prompt_cache_likely_expired`, `estimated_cache_write_usd`.
- `PreCompact {trigger: manual|auto, custom_instructions}`, `PostCompact {trigger, compact_summary}`.
- `SubagentStart {agent_id, agent_type}`, `SubagentStop {agent_id, agent_type, agent_transcript_path, last_assistant_message, background_tasks}`.
- `PermissionDenied {tool_name, tool_input, tool_use_id, reason}`.
- `TaskCreated / TaskCompleted {task_id, task_subject, task_description}`.
- `PostModelSwitch` (source command|picker|sdk|auto|resume), `CwdChanged {old_cwd, new_cwd}`, `WorktreeCreate {name}`, `WorktreeRemove {worktree_path}`.

## Status line input
csb-statusline.py stores only `rate_limits`, in `usage.json`, observed as `{five_hour, seven_day}: {used_percentage, resets_at}`. The schema also has a `context_window` block (total_input_tokens, total_output_tokens, context_window_size, current_usage, used_percentage, remaining_percentage) and cost fields (total_cost_usd, total_duration_ms, total_api_duration_ms, total_lines_added, total_lines_removed). Gateway setups get an extra `spend_limit`.

## Background sessions
- `claude agents --json --all` (documented; observed) returns only id, cwd, kind, startedAt, sessionId, name, state, plus pid and status while live.
- `~/.claude/jobs/<id>/state.json` (observed; internal and undocumented, so it may change): state, detail, `tempo` (active|idle|blocked), `inFlight {tasks, queued, kinds}`, tokens, `output.result` (the session's one-line outcome), `children[] {id, href, kind: pr|frame, title}`, worktreePath, worktreeBranch, `cliVersion`, createdAt, updatedAt. `timeline.jsonl` next to it records `{at, state, detail, text}`.
- CLI (documented): `claude logs <id>` (recent terminal output), `claude respawn <id>|--all` (restart on the current version), `claude rm <id>` (delete it and its worktree when that is safe).
