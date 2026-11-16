$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PidFile = Join-Path $ProjectRoot '.app.pid'
$AppPath = Join-Path $ProjectRoot 'app.py'

$Targets = @()
if (Test-Path $PidFile) {
    $SavedPid = 0
    if ([int]::TryParse((Get-Content -Raw $PidFile).Trim(), [ref]$SavedPid)) {
        $ProcessInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $SavedPid" -ErrorAction SilentlyContinue
        if ($ProcessInfo -and $ProcessInfo.CommandLine -match '(?i)python' -and $ProcessInfo.CommandLine.Contains($AppPath)) {
            $Targets += $ProcessInfo
        }
    }
}

$Targets += Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -and $_.CommandLine -match '(?i)python' -and $_.CommandLine.Contains($AppPath)
}
$Targets = @($Targets | Sort-Object ProcessId -Unique)
foreach ($Target in $Targets) {
    Stop-Process -Id $Target.ProcessId -ErrorAction SilentlyContinue
    try { Wait-Process -Id $Target.ProcessId -Timeout 15 -ErrorAction Stop }
    catch {
        if (Get-Process -Id $Target.ProcessId -ErrorAction SilentlyContinue) {
            throw "应用进程 $($Target.ProcessId) 未能在 15 秒内退出。"
        }
    }
    Write-Host "已停止应用进程 $($Target.ProcessId)。"
}
if (Test-Path $PidFile) { Remove-Item -LiteralPath $PidFile -Force }
if ($Targets.Count -eq 0) { Write-Host '没有找到由本脚本启动的应用进程。' }
