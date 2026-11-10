# Launches one unattended paper-trading session (open | midday | close).
# Called by the Windows Task Scheduler tasks "HedgeFund Autotrade *".
param([Parameter(Mandatory = $true)][ValidateSet("open", "midday", "close")][string]$Session)

$ErrorActionPreference = "Continue"
$project = Split-Path -Parent $PSScriptRoot
Set-Location $project
New-Item -ItemType Directory -Force "$project\logs" | Out-Null

$env:PYTHONUTF8 = "1"
$env:Path = "$env:LOCALAPPDATA\Programs\Python\Python312;$env:LOCALAPPDATA\Programs\Python\Python312\Scripts;$env:USERPROFILE\.local\bin;$env:Path"

$stamp = Get-Date -Format "yyyyMMdd_HHmm"
$log = "$project\logs\autotrade_${stamp}_$Session.log"
"=== $Session session started $(Get-Date -Format o) ===" | Out-File -FilePath $log -Encoding utf8

$tools = "Bash Read Write Edit Glob Grep Agent WebSearch WebFetch mcp__paper-broker"
# Out-File keeps the log UTF-8; plain *>> would write UTF-16 in Windows PowerShell 5
# Retry when the broker MCP connection drops at startup (seen 2026-10-05 09:33: CONNECTION_CLOSED,
# committee skipped). Up to 3 attempts, 30 s apart, only if the session reports that failure.
for ($attempt = 1; $attempt -le 3; $attempt++) {
    $out = & claude -p "/autotrade $Session" --mcp-config "$project\.mcp.json" --allowedTools $tools 2>&1 |
        ForEach-Object { "$_" }
    $out | Out-File -FilePath $log -Append -Encoding utf8
    if (($out -join "`n") -notmatch "CONNECTION_CLOSED|MCP.{0,40}(down|failed|unavailable)") { break }
    "--- attempt $attempt hit a broker MCP failure; retrying in 30 s ---" | Out-File -FilePath $log -Append -Encoding utf8
    Start-Sleep -Seconds 30
}

"=== finished $(Get-Date -Format o) exit $LASTEXITCODE ===" | Out-File -FilePath $log -Append -Encoding utf8

