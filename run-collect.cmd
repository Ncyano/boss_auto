@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM ---- 管理员提权（注入鼠标键盘必需）----
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo  需要管理员权限，正在提权...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

set PYTHONIOENCODING=utf-8
if "%~1"=="" (set LIMIT=20) else (set LIMIT=%~1)

echo ============================================================
echo  采集 %LIMIT% 位候选人（有效人次）
echo  间隔 15~25 秒；整屏跳过时只向下滚，不刷新
echo  需要复核的人会自动存逐屏截图
echo.
echo  运行中可按 Ctrl+C 提前停止（已采集的结果会保留）
echo ============================================================
echo.

".venv\Scripts\python.exe" "boss_vision\driver.py" run --live --limit %LIMIT% --min-gap 15 --max-gap 25

echo.
echo ============================================================
echo  退出码 %ERRORLEVEL%   0=正常  3=关不掉弹窗  4=没有输入权限
echo  结果目录：boss_vision\runs\
echo.
echo  跑完可以执行：
echo    .venv\Scripts\python.exe boss_vision\export_csv.py
echo    .venv\Scripts\python.exe tools\screen_resumes.py
echo ============================================================
pause
