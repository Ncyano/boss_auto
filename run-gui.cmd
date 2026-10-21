@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM ---- 必须以管理员身份运行，否则无法向浏览器注入鼠标/键盘事件 ----
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo.
  echo  需要管理员权限（否则点不动浏览器）。
  echo  正在申请提权，请在弹窗里点「是」...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

echo ============================================================
echo  BOSS 推荐牛人采集 - 图形界面
echo  以管理员身份运行：OK
echo.
echo  运行前请确认：
echo    1. BOSS直聘页面已打开，停在「推荐牛人」列表
echo    2. 浏览器缩放保持 100%%（Ctrl+0）
echo    3. 不要最小化浏览器窗口
echo ============================================================
echo.

REM 用 python.exe（带控制台）启动，出错时能看到报错；GUI 关闭后控制台保留
start "BOSS采集控制台" cmd /k ""%~dp0.venv\Scripts\python.exe" "%~dp0boss_vision\gui.py""
exit /b
