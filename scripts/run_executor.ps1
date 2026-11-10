# Starts the execution desk (strategy_lab/executor.py) for the trading day.
# Called by the Windows task "HedgeFund Executor" at 9:44 AM ET; the script stops itself at 15:56.
$ErrorActionPreference = "Continue"
$project = Split-Path -Parent $PSScriptRoot
Set-Location $project
New-Item -ItemType Directory -Force "$project\logs" | Out-Null
$env:PYTHONUTF8 = "1"
$python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
& $python -m strategy_lab.executor 2>&1 | ForEach-Object { "$_" } |
    Out-File -FilePath "$project\logs\executor_console_$(Get-Date -Format yyyyMMdd).log" -Append -Encoding utf8
