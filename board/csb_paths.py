"""Where the session board keeps its data. One answer for every script (board, hooks, status line).

Order: $CSB_DIR; on Windows %USERPROFILE%\\.claude\\session-board; under WSL the Windows-side folder when a board
already runs there (so Windows and WSL sessions share one board); else ~/.claude/session-board.
bin/ct lives outside this folder and carries a copy of find_dir() - tests/test_paths.py checks they agree."""
import glob, os, shutil

def is_wsl():
    try:
        with open("/proc/version", encoding="utf-8", errors="replace") as f:
            return os.name != "nt" and "microsoft" in f.read().lower()
    except OSError:
        return False

def windows_homes():
    """Windows user folders (/mnt/c/Users/<you>) as seen from WSL; empty anywhere else"""
    return sorted(glob.glob("/mnt/c/Users/*")) if is_wsl() else []

def find_dir():
    if os.environ.get("CSB_DIR"):
        return os.environ["CSB_DIR"]
    if os.name == "nt":
        return os.path.join(os.environ["USERPROFILE"], ".claude", "session-board")
    for h in windows_homes():
        d = os.path.join(h, ".claude", "session-board")
        if os.path.isdir(os.path.join(d, "sessions")):
            return d
    return os.path.expanduser("~/.claude/session-board")

def has_tabs():
    """can the board open and switch Windows Terminal tabs (and call PowerShell helpers)? Windows itself, or WSL with
    wt.exe reachable. $CSB_TABS=0/1 overrides, for tests and unusual setups."""
    force = os.environ.get("CSB_TABS")
    if force in ("0", "1"): return force == "1"
    return os.name == "nt" or (is_wsl() and shutil.which("wt.exe") is not None)

if __name__ == "__main__":
    print(find_dir())
