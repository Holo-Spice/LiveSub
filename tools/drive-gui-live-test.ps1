# Drives the built launcher through one real live-capture run, so the floating subtitle
# window and the wait-for-speech line are exercised in the real GUI rather than assumed.
#
# ASCII only on purpose: Windows PowerShell 5.1 reads a .ps1 without a BOM as ANSI, so any
# literal Chinese in this file would be mangled before the parser ever sees it. The UI
# strings are built from code points instead.

param(
    [string]$Root = 'C:\Users\29279\LiveSub',
    [string]$Exe = 'C:\Users\29279\LiveSub\launcher\dist\LiveSub.Launcher.exe',
    [int]$StartupBudgetSeconds = 150,
    [string]$Output = 'gui-live-test.zh.srt'
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

function U([int[]]$codes) { -join ($codes | ForEach-Object { [char]$_ }) }

$TextLive = U 0x5B9E,0x65F6,0x7CFB,0x7EDF,0x97F3,0x9891          # 实时系统音频
$TextStart = U 0x5F00,0x59CB,0x91C7,0x96C6                        # 开始采集
$TextStop = U 0x505C,0x6B62,0x91C7,0x96C6                        # 停止采集
$TextWaiting = U 0x6B63,0x5728,0x7B49,0x5F85,0x8BED,0x97F3       # 正在等待语音
$TextStarting = U 0x6B63,0x5728,0x542F,0x52A8                     # 正在启动
$TextOverlay = U 0x004C,0x0069,0x0076,0x0065,0x0053,0x0075,0x0062,0x0020,0x5B9E,0x65F6,0x5B57,0x5E55   # LiveSub 实时字幕
$TextStopped = U 0x5DF2,0x505C,0x6B62                             # 已停止
$TextDevice = 'virtual-audio'

function Invoke-Node($node) {
    if (-not $node) { throw 'invoke target not found' }
    if (-not $node.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$null)) {
        $types = @($node.GetSupportedPatterns() | ForEach-Object { $_.ProgrammaticName })
        throw ('element "' + $node.Current.Name + '" has no invoke pattern; has: ' + ($types -join ', '))
    }
    $node.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
}

function Show-Buttons($root) {
    foreach ($node in $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)) {
        if ($node.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button) {
            Write-Host ('      button name="{0}" enabled={1} offscreen={2}' -f $node.Current.Name, $node.Current.IsEnabled, $node.Current.IsOffscreen)
        }
    }
}

function Find-ByName($root, $name) {
    $condition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, $name)
    return $root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition)
}

function Find-ByNamePrefix($root, $prefix) {
    foreach ($node in $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)) {
        if ($node.Current.Name -and $node.Current.Name.StartsWith($prefix)) { return $node.Current.Name }
    }
    return $null
}

function Find-DeviceCombo($root, $pattern) {
    foreach ($node in $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)) {
        if ($node.Current.ControlType -ne [System.Windows.Automation.ControlType]::ComboBox) { continue }
        foreach ($item in $node.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)) {
            if ($item.Current.Name -like $pattern) { return $node }
        }
    }
    return $null
}

function Describe-Combos($root) {
    foreach ($node in $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)) {
        if ($node.Current.ControlType -ne [System.Windows.Automation.ControlType]::ComboBox) { continue }
        $children = @($node.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition) | ForEach-Object { $_.Current.Name })
        Write-Host ('      combo name="{0}" enabled={1} children=[{2}]' -f $node.Current.Name, $node.Current.IsEnabled, ($children -join '; '))
        $patterns = @($node.GetSupportedPatterns() | ForEach-Object { $_.ProgrammaticName })
        Write-Host ('        patterns: ' + ($patterns -join ', '))
    }
    foreach ($node in $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)) {
        if ($node.Current.ControlType -eq [System.Windows.Automation.ControlType]::ListItem -or $node.Current.ControlType -eq [System.Windows.Automation.ControlType]::DataItem) {
            Write-Host ('      item: ' + $node.Current.Name)
        }
    }
}

function Select-Child($combo, $pattern) {
    foreach ($item in $combo.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)) {
        if ($item.Current.Name -like $pattern) {
            $item.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
            return $item.Current.Name
        }
    }
    return $null
}

function Overlay-Window() {
    return [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
        [System.Windows.Automation.TreeScope]::Children,
        (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::NameProperty, $TextOverlay))
    )
}

function Window-Text {
    param($root, [int]$limit = 400)
    $texts = @()
    foreach ($node in $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)) {
        if ($node.Current.Name) { $texts += $node.Current.Name }
        if ($texts.Count -ge $limit) { break }
    }
    return $texts
}

$ffmpeg = Join-Path $Root 'tools\ffmpeg\bin\ffmpeg.exe'
$ffplay = Join-Path $Root 'tools\downloads\ffmpeg-extracted\ffmpeg-9.0.1-essentials_build\bin\ffplay.exe'
$srt = Join-Path $Root ('outputs\live\' + $Output)
Remove-Item $srt, ($srt -replace '\.srt$', '.jsonl') -ErrorAction SilentlyContinue

$player = $null
$launcher = $null
try {
    Write-Host '[1] play the Japanese sample on the default output device (the loopback captures it)'
    $player = Start-Process -FilePath $ffplay -PassThru -WindowStyle Minimized -ArgumentList @(
        '-hide_banner', '-loglevel', 'error', '-nodisp', '-autoexit', '-loop', '0',
        (Join-Path $Root 'testdata\source-ja.flac')
    )

    Write-Host '[2] start the launcher'
    $launcher = Start-Process -FilePath $Exe -PassThru -ArgumentList @('--existing-root', $Root)
    Start-Sleep -Seconds 6

    $window = $null
    $deadline = (Get-Date).AddSeconds(30)
    while (-not $window -and (Get-Date) -lt $deadline) {
        $window = [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
            [System.Windows.Automation.TreeScope]::Children,
            (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty, $launcher.Id))
        )
        if (-not $window) { Start-Sleep -Milliseconds 500 }
    }
    if (-not $window) { throw 'launcher window not found' }
    Write-Host ('    window: ' + $window.Current.Name)

    Write-Host '[3] switch to live system audio'
    Show-Buttons $window
    $live = Find-ByName $window $TextLive
    if (-not $live) { throw 'live mode element not found' }
    Invoke-Node $live
    Start-Sleep -Seconds 4
    Write-Host '    window text after switching:'
    foreach ($text in (Window-Text $window)) { Write-Host ('      | ' + $text) }
    Describe-Combos $window

    Write-Host '[3b] click refresh to force device enumeration'
    $refresh = Find-ByName $window (U 0x5237,0x65B0)
    if ($refresh) { Invoke-Node $refresh } else { Write-Host '    refresh button not found' }
    Start-Sleep -Seconds 8
    Write-Host '    full text after refresh:'
    foreach ($text in (Window-Text $window)) { Write-Host ('      | ' + $text) }
    Describe-Combos $window

    $combo = Find-DeviceCombo $window ('*' + $TextDevice + '*')
    if (-not $combo) { throw 'device combo containing virtual-audio not found' }
    $picked = Select-Child $combo ('*' + $TextDevice + '*')
    Write-Host ('    device: ' + $picked)
    Start-Sleep -Seconds 1

    Write-Host '[4] click start'
    $button = Find-ByName $window $TextStart
    if (-not $button) { throw 'start button not present (input did not validate)' }
    if (-not $button.Current.IsEnabled) { throw 'start button is disabled' }
    Invoke-Node $button

    Write-Host '[5] wait for the floating subtitle window'
    $seen = New-Object System.Collections.Generic.List[string]
    $overlay = $null
    $deadline = (Get-Date).AddSeconds($StartupBudgetSeconds)
    while (-not $overlay -and (Get-Date) -lt $deadline) {
        $overlay = Overlay-Window
        if (-not $overlay) {
            $line = Find-ByNamePrefix $window $TextStarting
            if (-not $line) { $line = Find-ByNamePrefix $window $TextWaiting }
            if ($line -and -not $seen.Contains($line)) {
                $seen.Add($line)
                Write-Host ('    {0:HH:mm:ss} {1}' -f (Get-Date), $line)
            }
            Start-Sleep -Milliseconds 500
        }
    }

    if ($overlay) {
        Write-Host ('    floating window present: Offscreen={0}' -f $overlay.Current.IsOffscreen)
        for ($round = 1; $round -le 25; $round++) {
            Start-Sleep -Seconds 2
            $caption = ''
            foreach ($text in (Window-Text $overlay)) {
                if ($text -eq $TextOverlay) { continue }
                if ($text -like ('LiveSub ' + '*')) { continue }
                if ($text -eq 'System') { continue }
                $caption = $text
            }
            if ($caption) {
                Write-Host ('    overlay text: ' + $caption)
                if ($caption -notlike ($TextStarting + '*') -and $caption -notlike ($TextWaiting + '*')) { break }
            } else {
                Write-Host '    (window present, no text yet)'
            }
        }
    } else {
        Write-Host '    floating window NOT detected'
    }

    Write-Host '[6] click stop'
    $stop = Find-ByName $window $TextStop
    if ($stop) { Invoke-Node $stop } else { Write-Host '    stop button not found' }
    Start-Sleep -Seconds 25

    Write-Host ('    floating window closed after stop: {0}' -f (-not (Overlay-Window)))
    Write-Host ('    final status line: {0}' -f (Find-ByNamePrefix $window $TextStopped))
    if (Test-Path $srt) {
        $lines = (Get-Content $srt -Encoding UTF8 | Where-Object { $_ -match '^\d+$' }).Count
        Write-Host ('    subtitles written: {0} -> {1}' -f $lines, $srt)
        Get-Content $srt -Encoding UTF8 | Select-Object -First 12 | ForEach-Object { '      ' + $_ }
    } else {
        Write-Host '    no SRT written'
    }
    $jsonl = $srt -replace '\.srt$', '.jsonl'
    if (Test-Path $jsonl) {
        $records = Get-Content $jsonl -Encoding UTF8 | ForEach-Object { $_ | ConvertFrom-Json }
        $stages = ($records | Where-Object { $_.stage } | Select-Object -ExpandProperty stage -Unique) -join ','
        Write-Host ('    JSONL records {0}, stage: {1}' -f $records.Count, $stages)
    }
}
finally {
    if ($launcher -and -not $launcher.HasExited) {
        $launcher.CloseMainWindow() | Out-Null
        Start-Sleep -Seconds 2
        if (-not $launcher.HasExited) { $launcher.Kill() }
    }
    if ($player -and -not $player.HasExited) { $player.Kill() }
    Start-Sleep -Seconds 1
    Get-Process -Name 'ffmpeg', 'ffplay', 'llama-server' -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_.StartTime -gt (Get-Date).AddMinutes(-20)) { $_.Kill() }
    }
    Write-Host 'cleanup done'
}
