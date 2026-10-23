#!/usr/bin/env python3
"""Install / update the Claude session board from this checkout. Works on Windows, WSL, macOS and Linux.

It only copies files: the board and its hook scripts into the session-board folder, the things the first-run setup can
offer (rules, the task-intake skill, the ct launcher, the cloud templates) into <session-board>/assets/, and a `csb`
launcher. It does not edit ~/.claude/settings.json or ~/.claude/CLAUDE.md and it creates no accounts, topics or tokens:
those are chosen, one by one, the first time the board starts (`csb`), and can be changed with `csb --setup`.

The session-board folder is $CSB_DIR, else (WSL, when a board already runs on the Windows side) that folder, else
~/.claude/session-board."""
import os, shutil, stat, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "board"))
from csb_paths import find_dir

def copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)

def main():
    if sys.version_info < (3, 8): sys.exit("Python 3.8 or newer is needed")
    data = find_dir(); n = 0
    for f in sorted(os.listdir(os.path.join(HERE, "board"))):
        if f.endswith((".py", ".ps1")) or f == "dashboard.html":
            copy(os.path.join(HERE, "board", f), os.path.join(data, f)); n += 1
    assets = {"config/rules.md": "rules.md", "skills/task-intake/SKILL.md": "task-intake/SKILL.md", "skills/task-intake/task-reviewer.md": "task-intake/task-reviewer.md", "bin/ct": "ct",
              "board/cloud/setup-template.sh": "cloud/setup-template.sh", "board/cloud/board-report.py": "cloud/board-report.py"}
    for src, rel in assets.items(): copy(os.path.join(HERE, src), os.path.join(data, "assets", rel)); n += 1
    main_py = os.path.join(data, "csb-tui.py")
    if os.name == "nt":
        launcher = os.path.join(data, "csb.cmd")
        with open(launcher, "w", newline="\r\n") as f: f.write(f'@echo off\r\n"{sys.executable}" "{main_py}" %*\r\n')
        hint = f"Start it with:  {launcher}   (add {data} to your PATH to type `csb`)"
    else:
        bin_dir = os.path.join(os.path.expanduser("~"), ".local", "bin"); launcher = os.path.join(bin_dir, "csb")
        os.makedirs(bin_dir, exist_ok=True)
        with open(launcher, "w") as f: f.write(f'#!/bin/sh\nexec "{sys.executable}" "{main_py}" "$@"\n')
        os.chmod(launcher, os.stat(launcher).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        on_path = any(os.path.realpath(d) == os.path.realpath(bin_dir) for d in os.environ.get("PATH", "").split(os.pathsep) if d)
        hint = "Start it with:  csb" if on_path else f"Start it with:  {launcher}   (add {bin_dir} to your PATH to type `csb`)"
    print(f"Installed {n} files into {data}\n{hint}\nThe first start asks what to switch on; nothing is enabled before that.")

if __name__ == "__main__":
    main()
