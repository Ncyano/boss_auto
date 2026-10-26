@echo off
REM 以调试端口启动 Edge，供 DSH 用 CDP 派发点击（光标不动）
REM 注意：会打开一个新的浏览器实例，需要重新登录 BOSS
chcp 65001 >nul
set PROFILE=%LOCALAPPDATA%\Microsoft\Edge\User Data
start "" "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" ^
  --remote-debugging-port=9222 ^
  --profile-directory=Default ^
  --user-data-dir="%PROFILE%"
echo 已尝试用调试端口启动 Edge（9222）
echo 验证：浏览器打开后运行  python -c "import sys;sys.path.insert(0,'boss_vision');import cdp;print(cdp.available())"
timeout /t 3 >nul
