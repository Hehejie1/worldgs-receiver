$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = (Resolve-Path (Join-Path $scriptDir "..\..")).Path
$buildRoot = Join-Path $projectRoot "build"
$browsersRoot = Join-Path $buildRoot "ms-playwright"

New-Item -ItemType Directory -Force -Path $browsersRoot | Out-Null

$pythonExe = $env:PYTHON_BIN
$pythonArgs = @()
if (-not $pythonExe) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $pythonExe = "py"
        $pythonArgs = @("-3")
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $pythonExe = "python"
    } else {
        throw "Python 3.9+ was not found."
    }
}

function Invoke-Python {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    & $pythonExe @pythonArgs @Args
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: $pythonExe $($pythonArgs + $Args -join ' ')"
    }
}

Set-Location $projectRoot

Invoke-Python -m pip install -e ".[desktop]"
$env:PLAYWRIGHT_BROWSERS_PATH = $browsersRoot
Invoke-Python -m playwright install firefox
Invoke-Python -m PyInstaller desktop/sidecar/receiver_sidecar.spec --noconfirm --clean
