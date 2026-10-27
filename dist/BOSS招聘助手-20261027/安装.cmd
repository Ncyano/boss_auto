@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo  BOSS Recruiter Assistant - Installer
echo ============================================================
echo.

REM ---- 1. find a usable Python ----
set "PYEXE="
for %%P in (python.exe) do if not defined PYEXE if exist "%%~$PATH:P" set "PYEXE=%%~$PATH:P"
if not defined PYEXE (
  py -3 -c "import sys;print(sys.executable)" >nul 2>&1 && set "PYEXE=py -3"
)
if not defined PYEXE (
  for %%D in (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "C:\Python313\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
  ) do if not defined PYEXE if exist %%D set "PYEXE=%%~D"
)
if not defined PYEXE (
  echo   [ERROR] Python not found. Please install Python 3.10+ from python.org
  echo           and tick "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)
echo   Using Python: %PYEXE%
%PYEXE% -c "import sys; assert sys.version_info >= (3,10), 'need 3.10+'"
if errorlevel 1 (
  echo   [ERROR] Python 3.10 or newer is required.
  pause
  exit /b 1
)

REM ---- 2. create venv ----
if not exist ".venv\Scripts\python.exe" (
  echo   Creating virtual environment ...
  %PYEXE% -m venv .venv
  if errorlevel 1 (
    echo   [ERROR] failed to create venv
    pause
    exit /b 1
  )
)

REM ---- 3. install dependencies ----
echo   Installing dependencies (first run may take a few minutes) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
  echo   [WARN] mirror failed, retrying with default index ...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo   [ERROR] dependency install failed. Check network.
    pause
    exit /b 1
  )
)

REM ---- 4. sanity check ----
".venv\Scripts\python.exe" -c "import numpy, cv2, PIL, openpyxl; print('  deps OK')"
if errorlevel 1 (
  echo   [ERROR] dependency check failed.
  pause
  exit /b 1
)

echo.
echo ============================================================
echo  Install finished.
echo.
echo  Next steps:
echo    1. Double-click  run-edge-debug.cmd   (starts browser with debug port)
echo    2. Log in to BOSS in that window
echo    3. Double-click  run-gui.cmd          (starts the console)
echo ============================================================
echo.
pause
