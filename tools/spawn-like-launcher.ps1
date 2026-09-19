# Reproduce the launcher's exact child-process shape: GUI-style parent with no console,
# stdin held open as an anonymous pipe, stdout/stderr redirected as pipes and drained
# asynchronously. Answers whether a slow or stalled start is caused by how it is spawned.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\spawn-like-launcher.ps1 -Probe imports
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\spawn-like-launcher.ps1 -Probe ui-bridge-fault
param(
    [string]$Root = 'C:\Users\29279\LiveSub',
    [ValidateSet('imports', 'preflight', 'ui-bridge', 'ui-bridge-fault', 'plain', 'stall0', 'stall1', 'stall2', 'stdinpoll')][string]$Probe = 'imports',
    [int]$StopAfterSeconds = 0,
    [switch]$CloseStdin
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $Root '.venv\Scripts\python.exe'
$faultFile = Join-Path $Root 'outputs\probe-startup-fault.txt'
# No double quotes inside: this is embedded in the child's command line.
$faultCode = "import faulthandler; faulthandler.dump_traceback_later(45, repeat=True, file=open(r'$faultFile','w')); from subtitle_cli.ui_bridge import main; raise SystemExit(main())"
$liveArgs = ' live --audio-device virtual-audio-capturer --source-lang ja --mt-model 7b --mt-device gpu --output "' + (Join-Path $Root 'outputs\spawn-probe.zh.srt') + '"'
$arguments = switch ($Probe) {
    'imports' { '-u "tools\probe_startup_imports.py"' }
    'preflight' { '-u "tools\probe_startup_imports.py" --preflight --fault-after 40' }
    'ui-bridge' { '-u -m subtitle_cli.ui_bridge' + $liveArgs }
    'ui-bridge-fault' { '-u -c "' + $faultCode + '"' + $liveArgs }
    'plain' { '-u -c "print(1)"' }
    'stall0' { '-u "tools\\probe-loader-stall.py"' }
    'stall1' { '-u "tools\\probe-loader-stall.py" --reader' }
    'stall2' { '-u "tools\\probe-loader-stall.py" --worker' }
    'stdinpoll' { '-u "tools\\probe-stdin-poll.py"' }
}

$info = New-Object System.Diagnostics.ProcessStartInfo
$info.FileName = $python
$info.WorkingDirectory = $Root
$info.UseShellExecute = $false
$info.CreateNoWindow = $true
$info.RedirectStandardInput = $true
$info.RedirectStandardOutput = $true
$info.RedirectStandardError = $true
$utf8 = New-Object System.Text.UTF8Encoding($false)
$info.StandardOutputEncoding = $utf8
$info.StandardErrorEncoding = $utf8
# StandardInputEncoding is .NET Core only; PowerShell 5.1 runs on .NET Framework.
$info.Arguments = $arguments
$info.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'

$clock = [System.Diagnostics.Stopwatch]::StartNew()
$process = [System.Diagnostics.Process]::Start($info)
$outTask = $process.StandardOutput.ReadToEndAsync()
$errTask = $process.StandardError.ReadToEndAsync()
if ($CloseStdin) { $process.StandardInput.Close() }
if ($StopAfterSeconds -gt 0) {
    $timer = New-Object System.Timers.Timer ($StopAfterSeconds * 1000)
    $timer.AutoReset = $false
    $action = { $process.StandardInput.WriteLine('{"command":"stop"}'); $process.StandardInput.Flush() }
    Register-ObjectEvent -InputObject $timer -EventName Elapsed -Action $action | Out-Null
    $timer.Start()
}
$process.WaitForExit()
$stderr = $errTask.Result
Write-Output $outTask.Result
if ($stderr) { Write-Output '--- stderr ---'; Write-Output $stderr }
Write-Output ('--- total {0:N3}s, exit {1} ---' -f $clock.Elapsed.TotalSeconds, $process.ExitCode)


