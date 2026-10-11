@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: virtual environment not found. Run INSTALL_V5_WINDOWS.bat first.
  pause
  exit /b 2
)

echo === Release integrity ===
.venv\Scripts\python.exe release_manifest.py verify
if errorlevel 1 goto :fail

echo === Syntax ===
.venv\Scripts\python.exe -m compileall -q .
if errorlevel 1 goto :fail

echo === Config (your .env) ===
.venv\Scripts\python.exe -c "from config import Config, load_env; load_env('.env'); e = Config.from_env().validate(); print('CONFIG OK' if not e else 'CONFIG ERRORS: ' + '; '.join(e)); raise SystemExit(1 if e else 0)"
if errorlevel 1 goto :fail

echo === Full test suite (about 2-4 minutes) ===
.venv\Scripts\python.exe tests\run_tests.py
if errorlevel 1 goto :fail

echo.
echo V5.1 VERIFICATION PASSED.
pause
exit /b 0

:fail
echo.
echo V5.1 verification FAILED. See the error above.
pause
exit /b 1
