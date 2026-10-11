@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: virtual environment not found. Run INSTALL_V5_WINDOWS.bat first.
  pause
  exit /b 2
)
echo === Slango Pro Trader - deployment ladder (DRY_RUN - DEMO - LIVE_PILOT - LIVE_FULL) ===
.venv\Scripts\python.exe rollout.py status
echo.
echo Promote only when every box is [x]:
echo   .venv\Scripts\python.exe rollout.py promote --to DEMO --by YOURNAME
echo   .venv\Scripts\python.exe rollout.py promote --to LIVE_PILOT --by YOURNAME --login REAL_LOGIN --server REAL_SERVER
echo   .venv\Scripts\python.exe rollout.py promote --to LIVE_FULL --by YOURNAME
pause
