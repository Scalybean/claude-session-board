#!/bin/bash
# Session board: paste this into your claude.ai/code environment's setup script.
# Reports every cloud session to your local dashboard. Needs ntfy.sh in the environment's allowed domains.
# Cloud sessions only run hooks from the repo, so this writes the hook into each repo's .claude/settings.local.json:
# now for repos already on disk, and through a post-checkout hook (git template) for every repo cloned later.
H=/opt/session-board; mkdir -p "$H/git-hooks"
cat > "$H/board-report.py" <<'BOARD_EOF'
{{BOARD_REPORT}}
BOARD_EOF
# the same short global rules and the task-intake skill as on the local machine
mkdir -p "$HOME/.claude" "$H/skill"
cat > "$HOME/.claude/CLAUDE.md" <<'CMD_EOF'
{{CLOUD_CLAUDE_MD}}
CMD_EOF
cat > "$H/skill/SKILL.md" <<'SKILL_EOF'
{{TASK_INTAKE_SKILL}}
SKILL_EOF
cat > "$H/add-hook.py" <<'ADD_EOF'
import json, os, sys
root = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
p = os.path.join(root, ".claude", "settings.local.json"); os.makedirs(os.path.dirname(p), exist_ok=True)
try: d = json.load(open(p))
except Exception: d = {}
cmd = "python3 /opt/session-board/board-report.py"
for ev in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Notification", "Stop", "StopFailure", "SessionEnd"):
    arr = d.setdefault("hooks", {}).setdefault(ev, [])
    if not any(h.get("command") == cmd for g in arr for h in g.get("hooks", [])):
        arr.append({"hooks": [{"type": "command", "command": cmd, "timeout": 10}]})
json.dump(d, open(p, "w"), indent=2)
import shutil
sk = os.path.join(root, ".claude", "skills", "task-intake"); os.makedirs(sk, exist_ok=True)
shutil.copy("/opt/session-board/skill/SKILL.md", os.path.join(sk, "SKILL.md"))
ex = os.path.join(root, ".git", "info", "exclude")
try:
    txt = open(ex).read() if os.path.exists(ex) else ""
    add = [x for x in (".claude/settings.local.json", ".claude/skills/task-intake/") if x not in txt]
    if add: open(ex, "a").write("\n" + "\n".join(add) + "\n")
except Exception:
    pass
ADD_EOF
# new clones get our post-checkout hook through git's template directory: it is copied into each new repo's
# .git/hooks, so the repo's own hooks (pre-commit etc.) keep working - unlike a global core.hooksPath
mkdir -p "$H/git-template/hooks"
cat > "$H/git-template/hooks/post-checkout" <<'PC_EOF'
#!/bin/sh
top=$(git rev-parse --show-toplevel 2>/dev/null) && python3 /opt/session-board/add-hook.py "$top" >/dev/null 2>&1
exit 0
PC_EOF
chmod +x "$H/git-template/hooks/post-checkout"
git config --system --unset core.hooksPath 2>/dev/null; git config --global --unset core.hooksPath 2>/dev/null
git config --system init.templateDir "$H/git-template" 2>/dev/null || git config --global init.templateDir "$H/git-template"
for g in $(find / -maxdepth 4 -name .git -type d -not -path '/proc/*' 2>/dev/null); do python3 "$H/add-hook.py" "$(dirname "$g")"; done
exit 0
