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

rem First run (e.g. a fresh download from GitHub): create the environment and install packages.
if not exist "%PY%" (
    echo First run - setting up the Python environment. This takes a few minutes...
    set "BASEPY="
    where py >nul 2>&1 && set "BASEPY=py -3"
    if not defined BASEPY where python >nul 2>&1 && set "BASEPY=python"
    if not defined BASEPY (
        echo [ERROR] Python 3.11 or newer is not installed.
        echo Install it from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^), then run this file again.
        pause
        exit /b 1
    )
    call %%BASEPY%% -m venv "%~dp0.venv"
    if not exist "%PY%" (
        echo [ERROR] Could not create the virtual environment.
        pause
        exit /b 1
    )
    "%PY%" -m pip install --upgrade pip
    "%PY%" -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo [ERROR] Installing the required packages failed.
        echo  - Check your internet connection and try again.
        echo  - If the message above says "filename or extension is too long", move this folder to a
        echo    short path such as C:\gst-reconciliation and run start_app.bat again.
        rmdir /s /q "%~dp0.venv"
        pause
        exit /b 1
    )
    echo Setup complete.
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
