param(
    [string]$Dotnet = 'dotnet',
    [string]$Python = 'python',
    [string]$Destination = (Join-Path $PSScriptRoot 'dist')
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
& $Python (Join-Path $PSScriptRoot 'build-core.py')
if ($LASTEXITCODE -ne 0) { throw 'CLI core package build failed.' }

New-Item -ItemType Directory -Path $Destination -Force | Out-Null
& $Dotnet publish (Join-Path $PSScriptRoot 'LiveSub.Launcher.csproj') -c Release -r win-x64 --self-contained true -p:DebugType=none -o $Destination
if ($LASTEXITCODE -ne 0) { throw 'Launcher publish failed.' }

$exe = Join-Path $Destination 'LiveSub.Launcher.exe'
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'Published executable is missing.' }
$sha = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath (Join-Path $Destination 'LiveSub.Launcher.exe.sha256') -Value "$sha  LiveSub.Launcher.exe" -Encoding ascii
Copy-Item -LiteralPath (Join-Path $root 'THIRD_PARTY_NOTICES.md') -Destination (Join-Path $Destination 'THIRD_PARTY_NOTICES.md') -Force
Write-Output $exe
