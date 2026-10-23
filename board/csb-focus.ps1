# Claude Sessions - bring a session's window to the front (used by the terminal board's `a` key)
#   -Mode tab : a Windows Terminal tab. Prints the tab titles as one JSON line, then reads one command line on stdin:
#               "find <marker>"  wait (up to 3 s) for the tab whose title is exactly <marker> (the board writes it to
#                                the session's own terminal), select it
#               "match <title>"  select the one tab whose title is <title> (with or without Claude Code's status glyph)
#               and prints {"ok":..., "was":"<title the tab had before>", "why":"..."}.
#   -Mode app : the Claude desktop app's window. Prints {"ok":..., "why":"..."}.
# Must stay compatible with Windows PowerShell 5.1. Only selects tabs and raises windows; never sends keystrokes.
param([string]$Mode = 'tab')

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false; [Console]::InputEncoding = [Console]::OutputEncoding } catch {}
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class CsbWin {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, IntPtr pid);
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool attach);
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
  public static bool Raise(IntPtr h) {
    if (IsIconic(h)) ShowWindow(h, 9);                      // SW_RESTORE
    // Windows only lets the foreground app hand focus on; sharing its input queue for a moment counts as that
    uint fg = GetWindowThreadProcessId(GetForegroundWindow(), IntPtr.Zero), me = GetCurrentThreadId();
    bool joined = fg != 0 && fg != me && AttachThreadInput(me, fg, true);
    SetForegroundWindow(h); BringWindowToTop(h);
    if (joined) AttachThreadInput(me, fg, false);
    return GetForegroundWindow() == h;
  }
}
'@
$AE = [System.Windows.Automation.AutomationElement]
$Scope = [System.Windows.Automation.TreeScope]

function Say($obj) { [Console]::Out.WriteLine(($obj | ConvertTo-Json -Compress)); [Console]::Out.Flush() }

function Tabs {
  # every tab of every Windows Terminal window, in window then tab order
  $out = New-Object System.Collections.ArrayList
  $byClass = New-Object System.Windows.Automation.PropertyCondition($AE::ClassNameProperty, 'CASCADIA_HOSTING_WINDOW_CLASS')
  $isTab = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, [System.Windows.Automation.ControlType]::TabItem)
  foreach ($w in $AE::RootElement.FindAll($Scope::Children, $byClass)) {
    $i = 0
    foreach ($t in $w.FindAll($Scope::Descendants, $isTab)) {
      [void]$out.Add([pscustomobject]@{ win = $w; hwnd = [IntPtr]$w.Current.NativeWindowHandle; i = $i; el = $t; name = [string]$t.Current.Name }); $i++
    }
  }
  return $out
}

function Plain([string]$s) { return ($s -replace '^[^\p{L}\p{N}]+', '').Trim() }   # drop Claude Code's leading status glyph

function Pick($tab) {
  $tab.el.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
  [void][CsbWin]::Raise($tab.hwnd)
}

if ($Mode -eq 'app') {
  function AppWindow { Get-Process -Name claude -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero -and $_.Path -notmatch '\\\.local\\|\\npm\\' } | Select-Object -First 1 }
  $p = AppWindow
  if (-not $p) {
    # closed to the tray, or not running: opening its claude:// link starts it or brings its window back
    try { Start-Process 'claude://' } catch { Say @{ ok = $false; why = 'The Claude desktop app is not installed' }; exit 0 }
    $end = (Get-Date).AddSeconds(8)
    while (-not $p -and (Get-Date) -lt $end) { Start-Sleep -Milliseconds 250; $p = AppWindow }
    if (-not $p) { Say @{ ok = $false; why = 'The Claude desktop app did not open a window' }; exit 0 }
  }
  if ([CsbWin]::Raise($p.MainWindowHandle)) { Say @{ ok = $true; why = '' } }
  else { Say @{ ok = $false; why = 'Windows kept the Claude app behind (it should be flashing in the taskbar)' } }
  exit 0
}

$before = @(Tabs)
Say @{ tabs = @($before | ForEach-Object { $_.name }) }
$cmd = [Console]::In.ReadLine()
if (-not $cmd) { exit 0 }
$verb, $arg = $cmd -split ' ', 2

if ($verb -eq 'find') {
  $end = (Get-Date).AddSeconds(3)
  while ((Get-Date) -lt $end) {
    foreach ($t in @(Tabs)) {
      if ($t.name -ne $arg) { continue }
      $was = ($before | Where-Object { $_.hwnd -eq $t.hwnd -and $_.i -eq $t.i } | Select-Object -First 1).name
      Pick $t
      Say @{ ok = $true; was = [string]$was; why = '' }; exit 0
    }
    Start-Sleep -Milliseconds 100
  }
  Say @{ ok = $false; was = ''; why = 'Its terminal tab was not found in Windows Terminal' }; exit 0
}

if ($verb -eq 'match') {
  $want = Plain $arg
  $hits = @($before | Where-Object { $want -and (Plain $_.name) -eq $want })
  if ($hits.Count -eq 1) { Pick $hits[0]; Say @{ ok = $true; was = $hits[0].name; why = '' }; exit 0 }
  $why = 'No Windows Terminal tab is titled like this session'
  if ($hits.Count -gt 1) { $why = 'More than one tab has this title' }
  Say @{ ok = $false; was = ''; why = $why }; exit 0
}
