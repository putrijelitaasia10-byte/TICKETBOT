@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 goto :error
)

echo [2/3] Installing dependencies...
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 goto :error

if not exist ".env" (
    echo.
    echo No .env file found. Copy .env.example to .env and paste your bot token first.
    goto :error
)

echo [3/3] Starting the ticket bot. Press Ctrl+C to stop.
".venv\Scripts\python.exe" bot.py
goto :end

:error
echo.
echo Setup failed. Make sure Python 3.10+ is installed and added to PATH.

:end
pause
