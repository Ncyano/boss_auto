@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM Require administrator: without it, input injection into the browser fails.
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo.
  echo   Need administrator rights to control the browser.
  echo   Asking for elevation - please click "Yes" in the popup...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

echo ============================================================
echo  BOSS recruiter collector - GUI
echo  Running as administrator: OK
echo.
echo  Before start:
echo    1. BOSS page open on the recommended-candidates list
echo    2. Browser zoom at 100%% (Ctrl+0)
echo    3. Do not minimize the browser window
echo ============================================================
echo.
for %%F in ("%~dp0boss_vision\cdp.py") do echo  cdp.py last modified: %%~tF
echo.

REM Launch with python.exe (console build) so tracebacks stay visible.
start "BOSS Collector Console" cmd /k ""%~dp0.venv\Scripts\python.exe" "%~dp0boss_vision\gui.py""
exit /b
