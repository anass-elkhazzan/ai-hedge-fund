# Tiny desktop helper: screenshot, click, keys, focus a window.
param([string]$Action, [int]$X = 0, [int]$Y = 0, [string]$Keys = "", [string]$Out = "")
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type @"
using System; using System.Runtime.InteropServices;
public class W {
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint x, uint y, uint d, UIntPtr e);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern void keybd_event(byte k, byte s, uint f, UIntPtr e);
}
"@
[W]::SetProcessDPIAware() | Out-Null
switch ($Action) {
  "shot" {
    $b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
    $bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
    $g = [System.Drawing.Graphics]::FromImage($bmp); $g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
    $small = New-Object System.Drawing.Bitmap $bmp, ([int]($b.Width / 2)), ([int]($b.Height / 2))
    $small.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
    "screen $($b.Width)x$($b.Height) saved at half size"
  }
  "click" { [W]::SetCursorPos($X, $Y) | Out-Null; Start-Sleep -Milliseconds 100; [W]::mouse_event(2,0,0,0,[UIntPtr]::Zero); [W]::mouse_event(4,0,0,0,[UIntPtr]::Zero); "clicked $X,$Y" }
  "keys" { [System.Windows.Forms.SendKeys]::SendWait($Keys); "sent $Keys" }
  "focus" {
    $p = Get-Process | Where-Object { $_.MainWindowTitle -and $_.ProcessName -like "*TradingView*" } | Select-Object -First 1
    if ($p) { [W]::keybd_event(0x12,0,0,[UIntPtr]::Zero); [W]::keybd_event(0x12,0,2,[UIntPtr]::Zero); [W]::ShowWindow($p.MainWindowHandle, 3) | Out-Null; [W]::SetForegroundWindow($p.MainWindowHandle) | Out-Null; "focused $($p.ProcessName): $($p.MainWindowTitle)" } else { "TradingView window not found" }
  }
}
