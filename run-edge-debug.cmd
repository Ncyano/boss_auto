@echo off
setlocal
set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" set "EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not exist "%EDGE%" (
  echo [ERROR] msedge.exe not found
  pause
  exit /b 1
)
set "PROFILE=%LOCALAPPDATA%\boss-cdp-profile"
echo Starting Edge (dedicated profile + debugging port 9222) ...
echo Profile: %PROFILE%
echo NOTE: this opens a separate Edge instance; log in to BOSS once in it.
start "" "%EDGE%" --remote-debugging-port=9222 --user-data-dir="%PROFILE%"
timeout /t 4 >nul
echo.
echo Verify:
echo   .venv\Scripts\python.exe -c "import sys;sys.path.insert(0,'boss_vision');import cdp;print(cdp.available())"
pause
