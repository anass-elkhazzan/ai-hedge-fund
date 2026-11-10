# Shows the paper account on TradingView Desktop.
# Rebuilds the "Claude Paper Trades" Pine script from the paper account, puts it on the
# clipboard, opens TradingView Desktop and shows the 3 keystrokes to update the chart.
# (One script does everything: TradingView's free plan allows only 2 indicators per chart.)
param([switch]$Demo)

$ErrorActionPreference = "Stop"
$project = Split-Path -Parent $PSScriptRoot
Set-Location $project
$env:PYTHONUTF8 = "1"
$python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
Add-Type -AssemblyName System.Windows.Forms

if ($Demo) {
    & $python -c "from paper_broker import tradingview as tv; tv.export('tradingview/demo_replay.db', 'tradingview/demo', ' (DEMO 30 Sep)')"
    $file = "$project\tradingview\demo\Claude Paper Trades.pine"
} else {
    & $python -c "from paper_broker import tradingview as tv; tv.export()"
    $file = "$project\tradingview\Claude Paper Trades.pine"
}
Get-Content -Raw -Encoding UTF8 $file | Set-Clipboard
Start-Process "explorer.exe" "shell:AppsFolder\TradingView.Desktop_n534cwy3pjxzj!TradingView.Desktop"

$text = "The latest paper trades are on your clipboard.`n`n" +
    "In TradingView Desktop:`n" +
    "1. Open the apps menu (grid icon, bottom right) > Pine Editor.`n" +
    "2. Make sure 'Claude Paper Trades' is the open script (script name menu at the top of the editor).`n" +
    "3. Click in the editor, press Ctrl+A, Ctrl+V, then Ctrl+S (save).`n`n" +
    "The chart updates: every BUY/SELL with its time, each result, the trade log and the balance panel.`n" +
    "Use an MNQ1! (or MES1!) chart on 5 minutes; 5D shows the whole week."
[System.Windows.Forms.MessageBox]::Show($text, "Update Claude Paper Trades on TradingView", "OK", "Information") | Out-Null
