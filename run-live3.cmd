@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
".venv\Scripts\python.exe" "boss_vision\driver.py" run --live --limit 3
echo.
echo exit code: %ERRORLEVEL%   (0=ok  3=cannot close modal  4=no input permission)
echo results: boss_vision\runs\
echo press any key to close...
pause >nul
