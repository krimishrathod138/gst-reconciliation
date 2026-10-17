@echo off
rem Show GST Reconciliation usernames and reset a forgotten password.
cd /d "%~dp0"
".venv\Scripts\python.exe" reset_password.py
echo.
pause
