@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo Expert AI Agent Pro Trader V5 - Windows Setup
echo ============================================================

where py >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python launcher ^(py^) was not found.
  echo Install Python 3.11 x64 and make sure the Python launcher is enabled.
  pause
  exit /b 2
)

py -3.11 --version
if errorlevel 1 (
  echo ERROR: Python 3.11 is required.
  pause
  exit /b 2
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating V5 virtual environment...
  py -3.11 -m venv .venv
  if errorlevel 1 goto :fail
)

echo Upgrading pip...
.venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :fail

echo Installing V5 dependencies...
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :fail

if not exist ".env" (
  echo Creating .env from .env.example. Defaults remain DRY_RUN.
  copy /Y ".env.example" ".env" >nul
  if errorlevel 1 goto :fail
)

echo Running syntax/config verification...
.venv\Scripts\python.exe -m compileall -q .
if errorlevel 1 goto :fail
.venv\Scripts\python.exe -c "from config import Config; Config.from_env().validate(); print('CONFIG OK')"
if errorlevel 1 goto :fail


echo.
echo ============================================================
echo SETUP COMPLETE

echo ============================================================
echo Review .env, keep AGENT_MODE=DRY_RUN, then run START_V5_WINDOWS.bat
pause
exit /b 0

:fail
echo.
echo SETUP FAILED. Check the error above.
pause
exit /b 1
