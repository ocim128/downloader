@echo off
title XDL server (127.0.0.1:7788)
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [xdl] .venv not found in this folder.
  echo [xdl] create it first:
  echo        python -m venv .venv
  echo        .venv\Scripts\pip install -e ".[dev]"
  pause
  exit /b 1
)

echo [xdl] stopping any previous server on port 7788 ...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /c:":7788" ^| findstr /c:"LISTENING"') do (
  echo [xdl]   killing PID %%a
  taskkill /f /t /pid %%a >nul 2>&1
)

echo [xdl] starting http://127.0.0.1:7788 ...
echo [xdl] a browser tab will open - close THIS window to stop the server.
".venv\Scripts\python.exe" -m xdl.ui %*

echo.
echo [xdl] server stopped.
pause
