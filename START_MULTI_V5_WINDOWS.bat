@echo off
REM Expert AI Agent Pro Trader V5.3 - one pinned engine per market (Blueprint: a watchlist of 2-5 liquid assets).
REM Usage:  START_MULTI_V5_WINDOWS.bat XAUUSD EURUSD US30
REM Each market gets its own data folder (data\SYMBOL) and log; the broker suffix (e.g. XAUUSDs) is auto-detected.
REM All instances share the magic number, so the portfolio engine sees total exposure, and they share
REM data\shared_risk.json, so the two-loss rule and the 15-minute cooldown apply to the whole account.
REM Fixed-symbol instances stay pinned; account-level AUTO selection is intentionally disabled here.
REM One STOP file in this folder stops new entries in every instance.
cd /d "%~dp0"
if "%~1"=="" (
  echo Usage: START_MULTI_V5_WINDOWS.bat SYMBOL1 SYMBOL2 ...   ^(max 5^)
  exit /b 1
)
set COUNT=0
for %%S in (%*) do (
  set /a COUNT+=1
  start "Pro Trader %%S" cmd /k "set SYSTEM__SYMBOL=%%S&& set SYSTEM__DATA_DIR=data\%%S&& .venv\Scripts\python.exe main.py"
)
echo Started %COUNT% market instance(s). Keep AGENT_MODE=DRY_RUN until each market's evidence report passes.
