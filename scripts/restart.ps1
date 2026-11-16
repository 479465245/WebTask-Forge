$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
& (Join-Path $PSScriptRoot 'stop.ps1')

$VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $VenvPython)) { throw '未找到项目虚拟环境，请先运行 .\scripts\init.ps1。' }
if (-not $env:TESSERACT_CMD) {
    $TesseractDefault = Join-Path $env:ProgramFiles 'Tesseract-OCR\tesseract.exe'
    if (Test-Path -LiteralPath $TesseractDefault) {
        $env:TESSERACT_CMD = (Resolve-Path -LiteralPath $TesseractDefault).Path
    }
}
if ($env:TESSERACT_CMD -and -not (Test-Path -LiteralPath $env:TESSERACT_CMD)) {
    throw "TESSERACT_CMD 指向的文件不存在：$env:TESSERACT_CMD"
}
$DataDir = Join-Path $ProjectRoot '.browser-data'
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$StdoutLog = Join-Path $ProjectRoot 'app.stdout.log'
$StderrLog = Join-Path $ProjectRoot 'app.stderr.log'
$AppPath = Join-Path $ProjectRoot 'app.py'
$Process = Start-Process -FilePath $VenvPython -ArgumentList ('"{0}"' -f $AppPath) -WorkingDirectory $ProjectRoot -RedirectStandardOutput $StdoutLog -RedirectStandardError $StderrLog -PassThru
Set-Content -Path (Join-Path $ProjectRoot '.app.pid') -Value $Process.Id -NoNewline

$Port = 8765
if ($env:BROWSER_DEMO_PORT) { $Port = [int]$env:BROWSER_DEMO_PORT }
$Ready = $false
for ($i = 0; $i -lt 30; $i++) {
    if ($Process.HasExited) {
        Remove-Item (Join-Path $ProjectRoot '.app.pid') -ErrorAction SilentlyContinue
        throw "应用启动失败，请查看 $StderrLog。"
    }
    try {
        $Response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -TimeoutSec 1 -UseBasicParsing
        if ($Response.StatusCode -ge 200 -and $Response.StatusCode -lt 500) { $Ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 1
}
if (-not $Ready) { throw "应用进程已启动（PID $($Process.Id)），但 30 秒内未能确认 http://127.0.0.1:$Port/ 已就绪。请查看 $StderrLog。" }
Write-Host "应用已启动（PID $($Process.Id)）：http://127.0.0.1:$Port/"
