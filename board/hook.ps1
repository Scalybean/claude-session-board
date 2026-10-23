# Claude Session Board - hook script
# Called by Claude Code hooks. Reads the hook event JSON from stdin, records the
# session's state in sessions\<session_id>.json, then rebuilds sessions.js for the dashboard.
# Must stay compatible with Windows PowerShell 5.1.

$ErrorActionPreference = 'SilentlyContinue'
$startMs = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()

$Base = $env:CSB_DIR
if (-not $Base) { $Base = Join-Path $env:USERPROFILE '.claude\session-board' }
$SessDir = Join-Path $Base 'sessions'
if (-not (Test-Path $SessDir)) { New-Item -ItemType Directory -Path $SessDir -Force | Out-Null }
$Utf8 = New-Object System.Text.UTF8Encoding $false

function Clip([string]$s, [int]$n) {
  if (-not $s) { return '' }
  $s = ($s -replace '\s+', ' ').Trim()
  if ($s.Length -gt $n) { return $s.Substring(0, $n - 1) + [char]0x2026 }
  return $s
}
function SetProp($obj, [string]$name, $value) {
  if ($obj.PSObject.Properties[$name]) { $obj.$name = $value } else { $obj | Add-Member -NotePropertyName $name -NotePropertyValue $value }
}
function WriteAtomic([string]$path, [string]$text) {
  $tmp = "$path.$PID.tmp"
  [System.IO.File]::WriteAllText($tmp, $text, $Utf8)
  Move-Item -LiteralPath $tmp -Destination $path -Force
}
function Clean-Command([string]$c) {
  # show the command that matters, not the "cd <folder> &&" or "VAR=value;" in front of it
  if (-not $c) { return '' }
  $prev = $null
  while ($prev -ne $c) {
    $prev = $c
    $c = $c -replace '^\s*(cd|pushd|Set-Location)\s+("[^"]*"|''[^'']*''|\S+)\s*(&&|;)\s*', ''
    $c = $c -replace '^\s*(export\s+)?[A-Za-z_][A-Za-z0-9_]*=("[^"]*"|''[^'']*''|\S*)\s*(&&|;)\s*', ''
  }
  return $c
}
function Describe-Tool($name, $inp) {
  if (-not $inp) { return $name }
  switch -Regex ($name) {
    '^(Edit|MultiEdit|Write|Read|NotebookEdit)$' { $f = $inp.file_path; if (-not $f) { $f = $inp.notebook_path }; if ($f) { return "$name " + (Split-Path $f -Leaf) } }
    '^Bash$'      { return 'Ran ' + (Clip (Clean-Command $inp.command) 70) }
    '^PowerShell$' { return 'Ran ' + (Clip (Clean-Command $inp.command) 70) }
    '^(Grep|Glob)$' { return "$name " + (Clip $inp.pattern 50) }
    '^WebFetch$'  { return 'Fetched ' + (Clip $inp.url 60) }
    '^WebSearch$' { return 'Searched ' + (Clip $inp.query 60) }
    '^(Task|Agent)$' { return 'Subagent: ' + (Clip $inp.description 60) }
  }
  return $name
}
function Find-ClaudeProcess {
  # Walk up from this hook process to the Claude Code process, to know later whether the session is still alive.
  try {
    $all = @{}
    Get-CimInstance Win32_Process -Property ProcessId,ParentProcessId,Name,CreationDate | ForEach-Object { $all[[int]$_.ProcessId] = $_ }
    $cur = $all[[int]$PID]
    for ($i = 0; $i -lt 8 -and $cur; $i++) {
      $parent = $all[[int]$cur.ParentProcessId]
      if (-not $parent) { break }
      if ($parent.Name -match '^(claude|node)(\.exe)?$') {
        return @{ pid = [int]$parent.ProcessId; started = ([DateTimeOffset]$parent.CreationDate).ToUnixTimeMilliseconds() }
      }
      $cur = $parent
    }
  } catch {}
  return $null
}

function Resolve-Transcript([string]$p) {
  if (-not $p) { return $null }
  if (Test-Path -LiteralPath $p) { return $p }
  if ($p.StartsWith('/')) {
    # a WSL session: reach its files through the \\wsl.localhost share
    if (-not $env:WSL_DISTRO_NAME) { return $null }
    return '\\wsl.localhost\' + $env:WSL_DISTRO_NAME + ($p -replace '/', '\')
  }
  return $p
}
function Unescape-Lite([string]$s) {
  # decode JSON string escapes in one pass, tolerant of text cut off in the middle
  if (-not $s) { return '' }
  return [regex]::Replace($s, '\\(u[0-9a-fA-F]{4}|.)', {
    param($m)
    $c = $m.Groups[1].Value
    if ($c.Length -eq 5) { return [string][char][Convert]::ToInt32($c.Substring(1), 16) }
    switch -CaseSensitive ($c) { 'n' { "`n" } 't' { ' ' } 'r' { '' } 'b' { '' } 'f' { '' } default { $c } }
  })
}
function First-Sentences([string]$s, [int]$n) {
  if (-not $s) { return '' }
  $s = ($s -split "`n`n")[0]                      # first paragraph only
  $s = $s -replace '\*\*|`', ''                   # drop markdown emphasis and code ticks
  return Clip $s $n
}
$TlAsst = '"type":\s*"text",\s*"text":\s*"(?<text>(?:[^"\\]|\\.){0,900})|"type":\s*"tool_use",\s*"id":\s*"[^"]*",\s*"name":\s*"(?<name>[^"]+)",\s*"input":\s*\{(?<input>.{0,700})'
$TlActs = '^(Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell|WebSearch|WebFetch|Agent|Task|Skill)$'
function Field([string]$frag, [string]$key) {
  if ($frag -match ('"' + $key + '":\s*"((?:[^"\\]|\\.)*)')) { return Unescape-Lite $Matches[1] }
  return $null
}
function Line-Items([string]$line, [bool]$isAsst) {
  # the steps in one transcript line, in order; patterns instead of a full parse, because lines can be megabytes long
  $out = New-Object System.Collections.ArrayList
  $ts = 0; if ($line -match '"timestamp":\s*"([^"]+)"') { try { $ts = ([DateTimeOffset]::Parse($Matches[1])).ToUnixTimeMilliseconds() } catch {} }
  if (-not $isAsst) {
    if ($line -match '"role":\s*"user",\s*"content":\s*"((?:[^"\\]|\\.){0,600})') {
      $txt = Unescape-Lite $Matches[1]
      if ($txt -and $txt -notmatch '^\s*<') { [void]$out.Add([ordered]@{ t = $ts; k = 'you'; x = (Clip $txt 140) }) }
    }
    return $out
  }
  foreach ($m in [regex]::Matches($line, $TlAsst)) {
    if ($m.Groups['text'].Success) {
      $x = First-Sentences (Unescape-Lite $m.Groups['text'].Value) 200
      if ($x) { [void]$out.Add([ordered]@{ t = $ts; k = 'say'; x = $x }) }
    } elseif ($m.Groups['name'].Value -match $TlActs) {
      $name = $m.Groups['name'].Value; $frag = $m.Groups['input'].Value
      $inp = [pscustomobject]@{ file_path = (Field $frag 'file_path'); notebook_path = (Field $frag 'notebook_path'); command = (Field $frag 'command'); description = (Field $frag 'description'); query = (Field $frag 'query'); url = (Field $frag 'url') }
      $x = Describe-Tool $name $inp
      $sk = Field $frag 'skill'; if ($name -eq 'Skill' -and $sk) { $x = 'Skill ' + $sk }
      [void]$out.Add([ordered]@{ t = $ts; k = 'act'; x = $x })
    }
  }
  return $out
}
function Read-Timeline([string]$path) {
  # Claude's own short updates, its key actions and your requests, from the end of the session transcript
  $result = @{ items = @(); title = $null }
  $fp = Resolve-Transcript $path
  if (-not $fp -or -not (Test-Path -LiteralPath $fp)) { return $result }
  $fs = $null; $total = 0; $read = 0; $take = 0
  try {
    $fs = New-Object System.IO.FileStream($fp, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    $total = $fs.Length
    $take = [Math]::Min($total, 4194304)
    [void]$fs.Seek(-$take, [System.IO.SeekOrigin]::End)
    $buf = New-Object byte[] $take
    while ($read -lt $take) { $r = $fs.Read($buf, $read, $take - $read); if ($r -le 0) { break }; $read += $r }
  } catch { return $result } finally { if ($fs) { $fs.Dispose() } }
  $lines = $Utf8.GetString($buf, 0, $read) -split "`n"
  $first = 0; if ($take -lt $total) { $first = 1 }   # first line may be cut in half
  $newest = New-Object System.Collections.ArrayList   # collected newest first
  for ($i = $lines.Count - 1; $i -ge $first; $i--) {
    $line = $lines[$i]
    if (-not $result.title -and $line -match '"aiTitle":\s*"((?:[^"\\]|\\.)*)"') { $result.title = Unescape-Lite $Matches[1]; continue }
    if ($newest.Count -ge 8) { if ($result.title) { break } else { continue } }
    if ($line -notmatch '"isSidechain":\s*false') { continue }        # skip subagents' own transcripts
    $isAsst = $line -match '"role":\s*"assistant"'
    if (-not $isAsst -and $line -notmatch '"role":\s*"user",\s*"content":\s*"') { continue }
    $found = @(Line-Items $line $isAsst)
    for ($j = $found.Count - 1; $j -ge 0; $j--) {
      $it = $found[$j]
      if ($newest.Count -gt 0 -and $newest[$newest.Count - 1].x -eq $it.x) { continue }
      [void]$newest.Add($it)
    }
  }
  $newest.Reverse()
  $n = $newest.Count
  if ($n -gt 8) { $result.items = @($newest.GetRange($n - 8, 8)) } else { $result.items = @($newest) }
  return $result
}
function Update-Tasks($s, $ev) {
  # keep the session's task list, from whichever task tool Claude Code uses
  $name = [string]$ev.tool_name; $in = $ev.tool_input
  $tasks = New-Object System.Collections.ArrayList
  foreach ($x in @($s.tasks)) { if ($x) { [void]$tasks.Add($x) } }
  if ($name -eq 'TodoWrite' -and $in.todos) {
    $tasks.Clear()
    $i = 0; foreach ($td in @($in.todos)) { $i++; [void]$tasks.Add([pscustomobject]@{ id = [string]$i; subject = (Clip ([string]$td.content) 120); active = (Clip ([string]$td.activeForm) 120); status = [string]$td.status }) }
  } elseif ($name -eq 'TaskCreate') {
    $resp = ($ev.tool_response | ConvertTo-Json -Depth 4 -Compress)
    $tid = $null; if ($resp -match '#(\d+)') { $tid = $Matches[1] } elseif ($resp -match '"id"\s*:\s*"?(\d+)') { $tid = $Matches[1] }
    if (-not $tid) { $tid = [string]($tasks.Count + 1) }
    [void]$tasks.Add([pscustomobject]@{ id = [string]$tid; subject = (Clip ([string]$in.subject) 120); active = (Clip ([string]$in.activeForm) 120); status = 'pending' })
  } elseif ($name -eq 'TaskUpdate' -and $in.taskId) {
    foreach ($x in @($tasks)) {
      if ([string]$x.id -eq [string]$in.taskId) {
        if ($in.status -eq 'deleted') { $tasks.Remove($x); break }
        if ($in.status) { $x.status = [string]$in.status }
        if ($in.subject) { $x.subject = Clip ([string]$in.subject) 120 }
        if ($in.activeForm) { $x.active = Clip ([string]$in.activeForm) 120 }
      }
    }
  } else { return }
  SetProp $s 'tasks' @($tasks)
}

# ---- read event ----
try { [Console]::InputEncoding = $Utf8 } catch {}
$raw = [Console]::In.ReadToEnd()
if (-not $raw) { exit 0 }
$ev = $raw | ConvertFrom-Json
if (-not $ev -or -not $ev.session_id) { exit 0 }
if ($ev.agent_id) { exit 0 }   # ignore subagent-scoped events; the parent session covers them

$id = [string]$ev.session_id
$evName = [string]$ev.hook_event_name
$file = Join-Path $SessDir ($id + '.json')

$mutex = New-Object System.Threading.Mutex -ArgumentList $false, ('csb_' + $id)
[void]$mutex.WaitOne(5000)
try {
  $s = $null
  if (Test-Path $file) { $s = [System.IO.File]::ReadAllText($file, $Utf8) | ConvertFrom-Json }
  if (-not $s) {
    $s = [pscustomobject]@{ id = $id; createdAt = $startMs; state = 'ready'; t = 0; events = 0 }
  }
  # Async hooks can finish out of order; never let an older event overwrite a newer state.
  $isNewer = ($startMs -ge [int64]$s.t)

  SetProp $s 'cwd' ([string]$ev.cwd)
  SetProp $s 'lastEventAt' ([Math]::Max([int64]$s.lastEventAt, $startMs))
  SetProp $s 'events' ([int]$s.events + 1)
  if ($ev.session_title) { SetProp $s 'name' (Clip $ev.session_title 80) }
  if ($ev.permission_mode) { SetProp $s 'mode' ([string]$ev.permission_mode) }
  if ($env:CSB_WSL_PID) { SetProp $s 'term' ('wsl:' + [string]$env:WSL_DISTRO_NAME + ':' + [string]$env:CSB_WSL_PID) }

  $newState = $null; $detail = $null
  switch ($evName) {
    'SessionStart' {
      $newState = 'ready'; $detail = 'Waiting for your first message'
      if ($ev.source -eq 'resume') { $detail = 'Resumed, waiting for you' }
      if ($ev.source -eq 'compact') { $newState = $null }
      if ($ev.model) { SetProp $s 'model' ([string]$ev.model) }
      SetProp $s 'endedAt' $null
      $p = Find-ClaudeProcess
      if ($p) { SetProp $s 'pid' $p.pid; SetProp $s 'pidStarted' $p.started; if (-not $env:CSB_WSL_PID) { SetProp $s 'term' ('win:' + $p.pid) } }
    }
    'UserPromptSubmit' {
      $newState = 'working'; $detail = 'Thinking'
      $prompt = Clip ([string]$ev.prompt) 160
      # messages Claude Code injects itself (task notifications, slash-command output) are not your requests
      if ($prompt -match '^<[A-Za-z_-]+>') { $prompt = '' }
      if ($prompt) {
        SetProp $s 'lastPrompt' $prompt
        if (-not $s.firstPrompt) { SetProp $s 'firstPrompt' $prompt }
        # short replies like "yes go ahead" don't describe the task; keep the last real request as the topic
        if ($prompt.Length -ge 25 -or -not $s.topic) { SetProp $s 'topic' $prompt }
      }
    }
    'PostToolUse' { $newState = 'working'; $detail = Describe-Tool $ev.tool_name $ev.tool_input }
    'PostToolUseFailure' { $newState = 'working'; $detail = (Describe-Tool $ev.tool_name $ev.tool_input) + ' (failed)' }
    'PermissionRequest' {
      $newState = 'needs'
      $tn = [string]$ev.tool_name
      if ($tn -eq 'AskUserQuestion') {
        # not a permission: Claude is asking you a multiple-choice question
        $q = [string](@($ev.tool_input.questions)[0].question)
        $detail = 'Asks: ' + (Clip $q 140); if (-not $q) { $detail = 'Asked you a question' }
      }
      elseif ($tn -match '^(Bash|PowerShell)$' -and $ev.tool_input.command) { $detail = 'Permission to run: ' + (Clip $ev.tool_input.command 120) }
      else { $detail = 'Permission to use ' + (Describe-Tool $tn $ev.tool_input) }
    }
    'Notification' {
      $nt = [string]$ev.notification_type
      $msg = Clip ([string]$ev.message) 160
      # agent_needs_input comes from background agents and forks ending their turn with a question;
      # the session that started them carries on, so it is not a reason to mark anything blocked
      if ($nt -match '^(permission_prompt|elicitation_dialog)$') {
        $newState = 'needs'; $detail = $msg
        if (-not $detail) { $detail = 'Waiting for your answer' }
        # keep a more specific PermissionRequest detail if we already have one
        if ($s.state -eq 'needs' -and $s.detail -like 'Permission *' -and $nt -eq 'permission_prompt') { $detail = $s.detail }
      }
      # idle_prompt just means "still your turn" - leave the state alone
    }
    'Stop' {
      $newState = 'yourturn'; $detail = 'Finished, your turn'
      $last = Clip ([string]$ev.last_assistant_message) 220
      if ($last) { SetProp $s 'lastReply' $last }
    }
    'StopFailure' { $newState = 'error'; $detail = Clip ('Stopped with an error ' + [string]$ev.error) 160 }
    'ChatReport' {
      # sent by the desktop chat add-on (mcp-server.ps1), not by Claude Code
      SetProp $s 'surface' 'chat'
      if ($ev.title) { SetProp $s 'name' (Clip ([string]$ev.title) 80) }
      if ($ev.steps) {
        # the chat's own step list, sent along with the report
        $map = @{ done = 'completed'; in_progress = 'in_progress'; not_started = 'pending'; stopped = 'pending' }
        $steps = @(); $i = 0
        foreach ($st in @($ev.steps)) { $i++; $stt = $map[[string]$st.status]; if (-not $stt) { $stt = 'pending' }; $steps += [pscustomobject]@{ id = [string]$i; subject = (Clip ([string]$st.title) 120); active = ''; status = $stt } }
        SetProp $s 'tasks' $steps
      }
      $sum = Clip ([string]$ev.summary) 200
      if ($sum) { SetProp $s 'timeline' @([ordered]@{ t = $startMs; k = 'say'; x = $sum }) }
      switch ([string]$ev.chat_state) {
        'working'     { $newState = 'working'; $detail = $sum; if (-not $detail) { $detail = 'Working' } }
        default       { $newState = 'yourturn'; $detail = 'Finished, your turn'; if ($sum) { SetProp $s 'lastReply' $sum } }
      }
    }
    'SessionEnd' {
      $newState = 'ended'; $detail = 'Session closed'
      SetProp $s 'endedAt' $startMs
    }
  }

  if ($evName -match '^PostToolUse' -and [string]$ev.tool_name -match '^(TodoWrite|TaskCreate|TaskUpdate)$') { Update-Tasks $s $ev }
  if ($ev.transcript_path -and $evName -match '^(PostToolUse|UserPromptSubmit|Stop|PermissionRequest|Notification|SessionStart)$') {
    # re-read the transcript at most every 3 seconds per session; Stop always refreshes
    if ($evName -ne 'PostToolUse' -or ($startMs - [int64]$s.tlAt) -gt 3000) {
      $tl = Read-Timeline ([string]$ev.transcript_path)
      if ($tl.items.Count -gt 0) { SetProp $s 'timeline' $tl.items }
      if ($tl.title) { SetProp $s 'aiTitle' (Clip $tl.title 80) }
      SetProp $s 'tlAt' $startMs
    }
  }

  # a closed session only comes back if it is actually started or used again, not because of a stray notification
  if ($s.state -eq 'ended' -and $evName -notin @('SessionStart', 'UserPromptSubmit')) { $newState = $null }

  if ($newState -and $isNewer) {
    # a new reply starts a new wait, even when the row was already "your turn"
    if ($s.state -ne $newState -or ($newState -eq 'yourturn' -and $evName -in @('Stop', 'ChatReport'))) { SetProp $s 'stateSince' $startMs }
    SetProp $s 'state' $newState
    SetProp $s 'detail' $detail
    SetProp $s 't' $startMs
  } elseif ($newState -eq 'working' -and $s.state -eq 'working' -and $detail) {
    SetProp $s 'detail' $detail
  }
  if (-not $s.stateSince) { SetProp $s 'stateSince' $startMs }

  WriteAtomic $file ($s | ConvertTo-Json -Depth 8 -Compress)
} finally {
  $mutex.ReleaseMutex() | Out-Null
  $mutex.Dispose()
}

# ---- rebuild sessions.js (all sessions), pruning dead ones ----
$gm = New-Object System.Threading.Mutex -ArgumentList $false, 'csb_rebuild'
if ($gm.WaitOne(4000)) {
  try {
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
    $list = New-Object System.Collections.ArrayList
    foreach ($f in Get-ChildItem -LiteralPath $SessDir -Filter '*.json') {
      $o = $null
      try { $o = [System.IO.File]::ReadAllText($f.FullName, $Utf8) | ConvertFrom-Json } catch {}
      if (-not $o) { continue }
      $alive = $null
      if ($o.pid -and $o.state -ne 'ended') {
        $proc = Get-Process -Id ([int]$o.pid) -ErrorAction SilentlyContinue
        $alive = [bool]$proc
        if ($proc -and $o.pidStarted) {
          try { if ([Math]::Abs(([DateTimeOffset]$proc.StartTime).ToUnixTimeMilliseconds() - [int64]$o.pidStarted) -gt 5000) { $alive = $false } } catch {}
        }
        if ($alive -eq $false) {
          SetProp $o 'state' 'ended'; SetProp $o 'detail' 'Terminal closed'; SetProp $o 'endedAt' $now; SetProp $o 'stateSince' $now
          WriteAtomic $f.FullName ($o | ConvertTo-Json -Depth 8 -Compress)
        }
      }
      # forget ended sessions after 2 days
      if ($o.state -eq 'ended' -and $o.endedAt -and ($now - [int64]$o.endedAt) -gt 172800000) { Remove-Item -LiteralPath $f.FullName -Force; continue }
      [void]$list.Add($o)
    }
    # one terminal runs one session at a time: after /clear or a restart, older ids in the same terminal are over
    $latest = @{}
    foreach ($o in $list) { if ($o.term -and $o.state -ne 'ended') { $k = [string]$o.term; if (-not $latest[$k] -or [int64]$o.lastEventAt -gt [int64]$latest[$k].lastEventAt) { $latest[$k] = $o } } }
    foreach ($o in $list) {
      if ($o.term -and $o.state -ne 'ended' -and -not [object]::ReferenceEquals($latest[[string]$o.term], $o)) {
        SetProp $o 'state' 'ended'; SetProp $o 'detail' 'Replaced by a newer session in the same terminal'; SetProp $o 'endedAt' $now; SetProp $o 'stateSince' $now
        WriteAtomic (Join-Path $SessDir ($o.id + '.json')) ($o | ConvertTo-Json -Depth 8 -Compress)
      }
    }
    $json = '[]'
    if ($list.Count -gt 0) { $json = ConvertTo-Json -InputObject @($list.ToArray()) -Depth 8 -Compress }
    # the page reloads itself when dashboard.html changes, so an update shows up without pressing F5
    $pageAt = 0; try { $pageAt = (Get-Item -LiteralPath (Join-Path $Base 'dashboard.html')).LastWriteTimeUtc.Ticks } catch {}
    WriteAtomic (Join-Path $Base 'sessions.js') ("window.__CSB__ = { generatedAt: $now, pageAt: $pageAt, sessions: $json };")
  } finally { $gm.ReleaseMutex() | Out-Null; $gm.Dispose() }
}
exit 0
