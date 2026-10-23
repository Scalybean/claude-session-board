# Claude Sessions - desktop chat add-on (local MCP server, stdio)
# Gives Claude desktop chats one tool, report_status, which records the chat on the Claude Sessions board.
# Must stay compatible with Windows PowerShell 5.1. Never write anything to stdout except protocol messages.

$ErrorActionPreference = 'SilentlyContinue'
$Utf8 = New-Object System.Text.UTF8Encoding $false
try { [Console]::InputEncoding = $Utf8; [Console]::OutputEncoding = $Utf8 } catch {}

$Base = $env:CSB_DIR
if (-not $Base) { $Base = Join-Path $env:USERPROFILE '.claude\session-board' }
$Hook = Join-Path $Base 'hook.ps1'
$Exe = (Get-Process -Id $PID).Path

$Tool = [ordered]@{
  name = 'report_status'
  description = 'Show this chat on the user''s Claude Sessions dashboard. Every reply starts with a "working" call, before any other tool or text, even a short reply or a follow-up in a chat that already reported "done". Every reply ends with a "done" call. Without the "working" call the dashboard keeps showing the chat as finished while you work. Pass back the chat_id the first call returned.'
  inputSchema = [ordered]@{
    type = 'object'
    properties = [ordered]@{
      chat_id = [ordered]@{ type = 'string'; description = 'The id returned by your first report_status call in this conversation. Omit it on the first call.' }
      state   = [ordered]@{ type = 'string'; enum = @('working', 'done', 'needs_input'); description = 'working: the first call of every reply, before you do anything else. done: the last call of every reply (needs_input is accepted and treated as done).' }
      title   = [ordered]@{ type = 'string'; description = 'A short, stable name for this conversation (under 60 characters). Keep it the same across calls.' }
      summary = [ordered]@{ type = 'string'; description = 'One short line: what you are doing now, or what the reply delivered.' }
      steps   = [ordered]@{ type = 'array'; description = 'Optional. Your current step list for this task, in order, as you track it in your task list.'; items = [ordered]@{ type = 'object'; properties = [ordered]@{ title = [ordered]@{ type = 'string' }; status = [ordered]@{ type = 'string'; enum = @('done', 'in_progress', 'not_started', 'stopped') } }; required = @('title', 'status') } }
    }
    required = @('state', 'title')
  }
}

function Send($obj) {
  [Console]::Out.WriteLine(($obj | ConvertTo-Json -Depth 20 -Compress))
  [Console]::Out.Flush()
}
function Report($a) {
  $id = [string]$a.chat_id
  $id = ($id -replace '[^A-Za-z0-9_-]', '')
  if (-not $id) { $id = 'c' + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds().ToString() + (Get-Random -Maximum 999) }
  $evt = [ordered]@{
    session_id = 'chat-' + $id
    hook_event_name = 'ChatReport'
    chat_state = [string]$a.state
    title = [string]$a.title
    summary = [string]$a.summary
  }
  if ($a.steps) { $evt.steps = @($a.steps | ForEach-Object { [ordered]@{ title = [string]$_.title; status = [string]$_.status } }) }
  $json = $evt | ConvertTo-Json -Depth 6 -Compress
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $Exe
  $psi.Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$Hook`""
  $psi.UseShellExecute = $false
  $psi.RedirectStandardInput = $true
  $psi.CreateNoWindow = $true
  $p = [System.Diagnostics.Process]::Start($psi)
  $bytes = $Utf8.GetBytes($json)
  $p.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
  $p.StandardInput.Close()
  return $id
}

while ($true) {
  $line = [Console]::In.ReadLine()
  if ($null -eq $line) { break }
  if (-not $line.Trim()) { continue }
  $msg = $null
  try { $msg = $line | ConvertFrom-Json } catch { continue }
  if (-not $msg) { continue }
  $hasId = [bool]$msg.PSObject.Properties['id']
  switch ([string]$msg.method) {
    'initialize' {
      $ver = [string]$msg.params.protocolVersion
      if (-not $ver) { $ver = '2025-06-18' }
      Send ([ordered]@{ jsonrpc = '2.0'; id = $msg.id; result = [ordered]@{
        protocolVersion = $ver
        capabilities = [ordered]@{ tools = [ordered]@{} }
        serverInfo = [ordered]@{ name = 'session-board'; version = '1.0.0' }
        instructions = 'Use report_status to keep this chat visible on the user''s Claude Sessions dashboard: "working" as the very first call of every reply (follow-ups included), then "done" as the last step of every reply. Reuse the chat_id from the first call.'
      } })
    }
    'ping' { Send ([ordered]@{ jsonrpc = '2.0'; id = $msg.id; result = [ordered]@{} }) }
    'tools/list' { Send ([ordered]@{ jsonrpc = '2.0'; id = $msg.id; result = [ordered]@{ tools = @($Tool) } }) }
    'tools/call' {
      if ($msg.params.name -ne 'report_status') {
        Send ([ordered]@{ jsonrpc = '2.0'; id = $msg.id; error = [ordered]@{ code = -32602; message = 'Unknown tool' } })
        break
      }
      $a = $msg.params.arguments
      if (-not $a -or -not $a.state -or -not $a.title) {
        Send ([ordered]@{ jsonrpc = '2.0'; id = $msg.id; result = [ordered]@{ isError = $true; content = @([ordered]@{ type = 'text'; text = 'state and title are required.' }) } })
        break
      }
      $text = ''
      $isErr = $false
      try {
        $id = Report $a
        $text = "Recorded as $($a.state). chat_id: $id (pass this chat_id on every later report_status call in this conversation)."
        # a finished reply leaves the row green until the next "working" call, so ask for it on the next message
        if ($a.state -ne 'working') { $text += ' When the user sends their next message, call report_status with state "working" first, before anything else.' }
      } catch {
        $isErr = $true; $text = 'Could not update the dashboard: ' + $_.Exception.Message
      }
      Send ([ordered]@{ jsonrpc = '2.0'; id = $msg.id; result = [ordered]@{ isError = $isErr; content = @([ordered]@{ type = 'text'; text = $text }) } })
    }
    default {
      if ($hasId) { Send ([ordered]@{ jsonrpc = '2.0'; id = $msg.id; error = [ordered]@{ code = -32601; message = 'Method not found' } }) }
    }
  }
}
