@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo  20 candidates test
echo  limit=20  max-scrolls=4  gap=15-25s
echo  estimated 8-12 minutes. console log:
echo    boss_vision\runs\console-live20.log
echo  keep the BOSS window open. press Ctrl+C to stop (results are kept).
echo ============================================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$env:PYTHONIOENCODING='utf-8'; & '.venv\Scripts\python.exe' 'boss_vision\driver.py' run --live --limit 20 --max-scrolls 4 2>&1 | Tee-Object -FilePath 'boss_vision\runs\console-live20.log'"
echo.
echo exit code: %ERRORLEVEL%
echo results: boss_vision\runs\
echo press any key to close...
pause >nul
