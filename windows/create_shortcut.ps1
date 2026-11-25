# Creates a "Crypto Desk" shortcut (with its own icon) on the Desktop and in the Start menu. It launches VISUALIZER.bat.
$ErrorActionPreference = 'Stop'
$here   = Split-Path -Parent $MyInvocation.MyCommand.Path   # ...\windows
$root   = Split-Path -Parent $here                          # repository root
$target = Join-Path $here 'VISUALIZER.bat'
$icon   = Join-Path $root 'assets\crypto-desk.ico'
if (-not (Test-Path $target)) { Write-Host "Cannot find VISUALIZER.bat next to this script."; exit 1 }
$ws = New-Object -ComObject WScript.Shell
function New-DeskShortcut([string]$dir) {
    if (-not $dir -or -not (Test-Path $dir)) { return $null }
    $lnk = Join-Path $dir 'Crypto Desk.lnk'
    $s = $ws.CreateShortcut($lnk)
    $s.TargetPath       = $target
    $s.WorkingDirectory = $root
    $s.WindowStyle      = 7          # minimized: only the app window shows, the little server window stays in the taskbar
    $s.Description      = 'Crypto Desk - live control room'
    if (Test-Path $icon) { $s.IconLocation = "$icon,0" }
    $s.Save()
    return $lnk
}
$made = @()
$made += New-DeskShortcut ([Environment]::GetFolderPath('Desktop'))
$made += New-DeskShortcut ([Environment]::GetFolderPath('Programs'))
$made = $made | Where-Object { $_ }
if ($made.Count -eq 0) { Write-Host "Could not create a shortcut."; exit 1 }
foreach ($m in $made) { Write-Host "Created: $m" }
exit 0
