$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = (Resolve-Path (Join-Path $scriptDir "..\..")).Path
$buildRoot = Join-Path $projectRoot "build"
$browsersRoot = Join-Path $buildRoot "ms-playwright"

New-Item -ItemType Directory -Force -Path $browsersRoot | Out-Null

$pythonExe = $env:PYTHON_BIN
if (-not $pythonExe) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $pythonExe = "py"
        $pythonArgs = @("-3")
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $pythonExe = "python"
        $pythonArgs = @()
    } else {
        throw "Python 3.9+ was not found."
    }
} else {
    $pythonArgs = @()
}

Set-Location $projectRoot

& $pythonExe @pythonArgs -m pip install --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }

& $pythonExe @pythonArgs -m pip install -e ".[desktop]"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

$env:PLAYWRIGHT_BROWSERS_PATH = $browsersRoot
& $pythonExe @pythonArgs -m playwright install firefox
if ($LASTEXITCODE -ne 0) { throw "playwright install failed" }

& $pythonExe @pythonArgs -m PyInstaller desktop/sidecar/receiver_sidecar.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
