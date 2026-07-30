@echo off
setlocal

cd /d "%~dp0"

set VENV_DIR=.venv
set VENV_PYTHON=%VENV_DIR%\Scripts\python.exe
set INSTALL_MARKER=%VENV_DIR%\.worldgs_receiver_installed
set NEEDS_INSTALL=0
set RUN_PYTHON=
set BOOTSTRAP_PYTHON=
set "DEPS_CHECK=import fastapi, uvicorn, multipart, qrcode, yaml, playwright"
set "FIREFOX_CHECK=from pathlib import Path; from playwright.sync_api import sync_playwright; p=sync_playwright().start(); ok=Path(p.firefox.executable_path).exists(); p.stop(); raise SystemExit(0 if ok else 1)"

py -3 -c "%DEPS_CHECK%" >nul 2>nul
if not errorlevel 1 (
  set RUN_PYTHON=py -3
)

if "%RUN_PYTHON%"=="" (
  python -c "%DEPS_CHECK%" >nul 2>nul
  if not errorlevel 1 (
    set RUN_PYTHON=python
  )
)

if not "%RUN_PYTHON%"=="" (
  %RUN_PYTHON% -c "%FIREFOX_CHECK%" >nul 2>nul
  if errorlevel 1 (
    echo [WorldGS Receiver] Installing Playwright Firefox browser...
    %RUN_PYTHON% -m playwright install firefox
    if errorlevel 1 exit /b 1
  )
  echo [WorldGS Receiver] Using current Python environment.
  %RUN_PYTHON% -m worldgs_receiver.cli %*
  exit /b %errorlevel%
)

py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
  set BOOTSTRAP_PYTHON=py -3
)

if "%BOOTSTRAP_PYTHON%"=="" (
  python -c "import sys" >nul 2>nul
  if not errorlevel 1 (
    set BOOTSTRAP_PYTHON=python
  )
)

if "%BOOTSTRAP_PYTHON%"=="" (
  echo [WorldGS Receiver] Python 3.9+ was not found.
  echo Please install Python 3.9+ and try again.
  exit /b 1
)

if not exist "%VENV_PYTHON%" (
  echo [WorldGS Receiver] Creating Python virtual environment...
  %BOOTSTRAP_PYTHON% -m venv "%VENV_DIR%"
  if errorlevel 1 exit /b 1
  set NEEDS_INSTALL=1
)

if not exist "%VENV_PYTHON%" (
  echo [WorldGS Receiver] Failed to create virtual environment.
  echo Please install Python 3.9+ and try again.
  exit /b 1
)

if not exist "%INSTALL_MARKER%" set NEEDS_INSTALL=1

if exist "%INSTALL_MARKER%" (
  "%VENV_PYTHON%" -c "from pathlib import Path; marker=Path(r'%INSTALL_MARKER%'); sources=[Path('requirements.txt'), Path('pyproject.toml')]; raise SystemExit(0 if marker.exists() and marker.stat().st_mtime >= max(p.stat().st_mtime for p in sources) else 1)" >nul 2>nul
  if errorlevel 1 set NEEDS_INSTALL=1
)

"%VENV_PYTHON%" -c "%DEPS_CHECK%" >nul 2>nul
if errorlevel 1 set NEEDS_INSTALL=1

if "%NEEDS_INSTALL%"=="1" (
  echo [WorldGS Receiver] Installing receiver dependencies...
  "%VENV_PYTHON%" -m pip install -r requirements.txt
  if errorlevel 1 exit /b 1
  type nul > "%INSTALL_MARKER%"
) else (
  echo [WorldGS Receiver] Dependencies are ready.
)

"%VENV_PYTHON%" -c "%FIREFOX_CHECK%" >nul 2>nul
if errorlevel 1 (
  echo [WorldGS Receiver] Installing Playwright Firefox browser...
  "%VENV_PYTHON%" -m playwright install firefox
  if errorlevel 1 exit /b 1
)

echo [WorldGS Receiver] Starting local receiver...
"%VENV_PYTHON%" -m worldgs_receiver.cli %*
