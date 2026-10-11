@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: virtual environment not found. Run INSTALL_V5_WINDOWS.bat first.
  pause
  exit /b 2
)

echo === V5.6 per-market walk-forward research ===
echo MetaTrader 5 must be open and logged in to the broker account you will trade.
echo.
echo [1/3] Exporting 3 years of M5 history for every watchlist market...
.venv\Scripts\python.exe export_history.py --markets WATCHLIST --tf M5 --years 3 --out history
if errorlevel 1 goto :fail

echo [2/3] Walk-forward + stress + Monte Carlo per market (can take hours; signals are cached)...
.venv\Scripts\python.exe market_research.py --history history --folds 5 --workers 4 --publish
if errorlevel 1 goto :fail

echo [3/3] Cross-market correlation matrix from the same history...
.venv\Scripts\python.exe correlation_matrix.py --history history

echo.
echo DONE. Report: research\markets\MARKET_RESEARCH_REPORT.md
echo Evidence:    data\market_evidence.json
echo To trade ONLY markets that passed, set BRAIN__REQUIRE_MARKET_EVIDENCE=true in .env
pause
exit /b 0

:fail
echo.
echo Market research FAILED. See the error above.
pause
exit /b 1
