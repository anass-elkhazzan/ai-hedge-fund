# Keeps Windows from sleeping during US market hours so the autotrade sessions run on time.
# Started by the "HedgeFund KeepAwake" scheduled task at 9:35 AM ET on trading days; exits at 4:30 PM.
# The screen may still turn off; only system sleep is blocked.
Add-Type @"
using System; using System.Runtime.InteropServices;
public class Awake { [DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint f); }
"@
$ES_CONTINUOUS = [uint32]"0x80000000"; $ES_SYSTEM_REQUIRED = [uint32]"0x00000001"
$until = (Get-Date).Date.AddHours(16).AddMinutes(30)
"keep-awake started $(Get-Date -Format o), until $until" | Out-File (Join-Path (Split-Path -Parent $PSScriptRoot) "logs\keep_awake.log") -Append -Encoding utf8
while ((Get-Date) -lt $until) {
    [Awake]::SetThreadExecutionState($ES_CONTINUOUS -bor $ES_SYSTEM_REQUIRED) | Out-Null
    Start-Sleep -Seconds 60
}
[Awake]::SetThreadExecutionState($ES_CONTINUOUS) | Out-Null
"keep-awake ended $(Get-Date -Format o)" | Out-File (Join-Path (Split-Path -Parent $PSScriptRoot) "logs\keep_awake.log") -Append -Encoding utf8
