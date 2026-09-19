# Puts a clickable LiveSub icon on the desktop, so the UI is started by double clicking and
# never by typing a command. ASCII only: Windows PowerShell 5.1 reads a .ps1 without a BOM as
# ANSI, so Chinese literals in this file would be mangled before the parser sees them; the
# Non-ASCII legacy text is built from code points at runtime instead.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\make-desktop-shortcut.ps1
#
# The shortcut starts LiveSub.Launcher.exe with NO arguments: the program looks for a complete
# environment next to itself, so a double click is enough.

param(
    [string]$Root = (Split-Path $PSScriptRoot -Parent),
    [string]$Name = ''
)

$ErrorActionPreference = 'Stop'

function U([int[]]$codes) { -join ($codes | ForEach-Object { [char]$_ }) }

$target = Join-Path $Root 'LiveSub.Launcher.exe'
if (-not (Test-Path $target)) { throw "launcher not found: $target" }
if ($Name -eq '') { $Name = 'LiveSub' }

$desktop = [Environment]::GetFolderPath('Desktop')
$link = Join-Path $desktop ($Name + '.lnk')
$description = U 0x5B9E,0x65F6,0x5B57,0x5E55,0x0020,0x002B,0x0020,0x79BB,0x7EBF,0x89C6,0x9891,0x8F6C,0x5B57,0x5E55   # 实时字幕 + 离线视频转字幕

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($link)
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = $Root
$shortcut.Description = $description
$icon = Join-Path $Root 'launcher\Assets\livesub.ico'
$shortcut.IconLocation = if (Test-Path $icon) { "$icon,0" } else { "$target,0" }
$shortcut.Save()

# Some Windows shell configurations have been observed to save a new .lnk before persisting
# TargetPath. Reopen it once and retry so the script never reports a decorative but unusable link.
$verified = $shell.CreateShortcut($link)
if ($verified.TargetPath -ne $target) {
    $verified.TargetPath = $target
    $verified.Arguments = ''
    $verified.WorkingDirectory = $Root
    $verified.Description = $description
    $verified.IconLocation = if (Test-Path $icon) { "$icon,0" } else { "$target,0" }
    $verified.Save()
    $verified = $shell.CreateShortcut($link)
}
if ($verified.TargetPath -ne $target) { throw 'shortcut target was not saved' }

# Remove the exact legacy shortcut only after the new English shortcut was saved.
$legacyName = U 0x004C,0x0069,0x0076,0x0065,0x0053,0x0075,0x0062,0x0020,0x5B57,0x5E55
$legacyLink = Join-Path $desktop ($legacyName + '.lnk')
if ($legacyLink -ne $link -and (Test-Path -LiteralPath $legacyLink)) {
    Remove-Item -LiteralPath $legacyLink
}

Remove-Variable shell
[Runtime.InteropServices.Marshal]::ReleaseComObject($shortcut) | Out-Null
[Runtime.InteropServices.Marshal]::ReleaseComObject($verified) | Out-Null

if (Test-Path $link) {
    Write-Host ('created: ' + $link)
    Write-Host ('target : ' + $target)
    Write-Host ('workdir: ' + $Root)
} else {
    throw 'shortcut was not created'
}
