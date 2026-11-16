$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $ProjectRoot

$Python = $null
$PythonArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 --version *> $null
    if ($LASTEXITCODE -eq 0) { $Python = 'py'; $PythonArgs = @('-3') }
}
if (-not $Python -and (Get-Command python -ErrorAction SilentlyContinue)) {
    $Python = 'python'
}
if (-not $Python) { throw '未找到 Python。请先安装 Python 3.10+ 并加入 PATH。' }

$Version = & $Python @PythonArgs -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
$Parts = $Version.Split('.')
if ([int]$Parts[0] -lt 3 -or ([int]$Parts[0] -eq 3 -and [int]$Parts[1] -lt 10)) {
    throw "需要 Python 3.10 或更高版本，当前为 $Version。"
}

$VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $VenvPython)) {
    & $Python @PythonArgs -m venv (Join-Path $ProjectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw '创建 Python 虚拟环境失败。' }
}
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw '升级 pip 失败。' }
& $VenvPython -m pip install -r (Join-Path $ProjectRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw '安装 Python 依赖失败。' }
& $VenvPython -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw '安装 Playwright Chromium 失败。' }

if (-not (Get-Command tesseract -ErrorAction SilentlyContinue) -and -not $env:TESSERACT_CMD) {
    Write-Warning '未找到 Tesseract OCR。请按 README 安装 Tesseract，并选择 English 与 Chinese (Simplified) 语言包。'
}
Write-Host '环境初始化完成。运行 .\scripts\restart.ps1 启动应用。'
Write-Host '请确认 config.py 中的 PostgreSQL DATABASE_URL 可连接。'
