@echo off
rem Starts Konus with a console attached (handy when something goes wrong).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run setup.bat first.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" main.py %*
