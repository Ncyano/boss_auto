@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

echo ============================================================
echo  自检：本进程完整性 / BOSS 窗口完整性 / 注入权限
echo  （只读检查，不点击任何东西）
echo ============================================================
echo.

".venv\Scripts\python.exe" "boss_vision\diag_input.py" --check

echo.
echo 提示：
echo   如果显示「本进程完整性=Low」，请右键本文件 - 以管理员身份运行
echo ============================================================
pause
