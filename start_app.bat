@echo off
rem ---------------------------------------------------------------------------
rem  GST Reconciliation - start the app and open it in the browser.
rem  Runs from this folder so .streamlit\config.toml (localhost-only) is used.
rem  Close this window (or press Ctrl+C) to stop the app.
rem ---------------------------------------------------------------------------
setlocal
title GST Reconciliation
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
set "URL=http://localhost:8510"
set "HEALTH=http://127.0.0.1:8510/_stcore/health"

if not exist "%PY%" (
    echo [ERROR] Virtual environment not found: "%PY%"
    echo Create it first:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

rem Already running? Just open the browser.
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing '%HEALTH%' -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }"
if %errorlevel%==0 (
    echo GST Reconciliation is already running - opening %URL%
    start "" "%URL%"
    exit /b 0
)

rem Open the browser as soon as the server answers (waits up to 60 seconds).
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "for ($i = 0; $i -lt 60; $i++) { try { Invoke-WebRequest -UseBasicParsing '%HEALTH%' -TimeoutSec 1 | Out-Null; Start-Process '%URL%'; break } catch { Start-Sleep -Seconds 1 } }"

echo Starting GST Reconciliation at %URL%  (localhost only)
echo Close this window to stop the app.
echo.
"%PY%" -m streamlit run app.py --server.headless true

echo.
echo The app has stopped.
pause
