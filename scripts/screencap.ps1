# Screen-capture helper for the lens debugging.
# Usage:
#   pwsh scripts/screencap.ps1                 # capture monitor 2 → /tmp/screencap.png
#   pwsh scripts/screencap.ps1 -Monitor 1      # capture monitor 1
#   pwsh scripts/screencap.ps1 -Output ...     # custom output path
param(
    [int]$Monitor = 2,
    [string]$Output = "C:\Users\semis\AppData\Local\Temp\lens_screencap.png"
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$screens = [System.Windows.Forms.Screen]::AllScreens
if ($Monitor -lt 1 -or $Monitor -gt $screens.Count) {
    Write-Error "Monitor $Monitor not available; have $($screens.Count) screen(s)."
    exit 1
}
$target = $screens[$Monitor - 1]
$bounds = $target.Bounds

$bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($bounds.X, $bounds.Y, 0, 0, $bmp.Size)

# Ensure parent dir exists.
$dir = Split-Path $Output -Parent
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }

$bmp.Save($Output, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose()
$bmp.Dispose()

Write-Host "Saved monitor $Monitor ($($bounds.Width)x$($bounds.Height)) to $Output"
