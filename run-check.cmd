@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
".venv\Scripts\python.exe" "boss_vision\diag_input.py" --check
echo.
echo press any key to close...
pause >nul
