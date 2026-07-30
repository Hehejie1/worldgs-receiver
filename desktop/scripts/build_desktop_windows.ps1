$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = (Resolve-Path (Join-Path $scriptDir "..\..")).Path
$desktopRoot = Join-Path $projectRoot "desktop"
$binariesRoot = Join-Path $desktopRoot "src-tauri\binaries"
$distBin = Join-Path $projectRoot "dist\receiver_sidecar.exe"

& (Join-Path $scriptDir "build_sidecar_windows.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "Failed to build Receiver sidecar."
}

$hostTriple = (& rustc --print host-tuple) 2>$null
if (-not $hostTriple) {
    $hostTriple = (& rustc -Vv | Select-String "host:" | ForEach-Object { $_.Line.Split(" ")[1] })
}
if (-not $hostTriple) {
    throw "Failed to determine rust target triple."
}

New-Item -ItemType Directory -Force -Path $binariesRoot | Out-Null
$targetBin = Join-Path $projectRoot "desktop\src-tauri\binaries\receiver_sidecar-$hostTriple.exe"
Copy-Item -Force $distBin $targetBin

Set-Location $desktopRoot
npm install
npx tauri build --bundles msi,nsis
if ($LASTEXITCODE -ne 0) {
    throw "Tauri build failed."
}
