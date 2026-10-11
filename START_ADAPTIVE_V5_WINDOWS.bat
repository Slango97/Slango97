@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo V5 virtual environment not found.
  echo Run: py -3.11 -m venv .venv
  echo Then: .venv\Scripts\python.exe -m pip install -r requirements.txt
  pause
  exit /b 2
)
if not exist ".env" (
  echo .env not found. Creating from .env.example...
  copy /Y ".env.example" ".env" >nul
  echo Review .env before running.
)
echo Starting ACCOUNT-LEVEL ADAPTIVE MARKET SELECTION (SYSTEM__SYMBOL=AUTO).
.venv\Scripts\python.exe main.py
endlocal
