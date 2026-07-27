@echo off
setlocal

echo.
echo  Stats++ Launcher
echo  =================
echo.

:: Check for Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    where python3 >nul 2>nul
    if %errorlevel% neq 0 (
        echo  [ERROR] Python not found.
        echo.
        echo  Stats++ requires Python 3.10 or newer.
        echo  Download it from: https://www.python.org/downloads/
        echo.
        echo  During installation, make sure to check "Add Python to PATH".
        echo.
        pause
        exit /b 1
    )
    set PYTHON=python3
) else (
    set PYTHON=python
)

:: Verify Python version
%PYTHON% -c "import sys; exit(0 if sys.version_info >= (3, 10) else 1)" 2>nul
if %errorlevel% neq 0 (
    echo  [ERROR] Python 3.10+ required.
    echo.
    %PYTHON% --version
    echo.
    echo  Download the latest version from: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

:: Create virtual environment if it doesn't exist
if not exist ".venv" (
    echo  Creating virtual environment...
    %PYTHON% -m venv .venv
    if %errorlevel% neq 0 (
        echo  [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

:: Install/update dependencies
echo  Checking dependencies...
.venv\Scripts\pip install -q -r requirements.txt 2>nul
if %errorlevel% neq 0 (
    echo  Installing dependencies...
    .venv\Scripts\pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo  [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
)

:: Launch the app
echo.
echo  Starting Stats++...
echo  Open your browser to: http://localhost:5001
echo.
echo  Press Ctrl+C to stop the server.
echo.

:: Open browser after a short delay
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:5001"

:: Run the server
.venv\Scripts\python web\app.py

pause
