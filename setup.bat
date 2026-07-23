@echo off
echo ======================================================================
echo   🛡️ SENTINEL AI TRADING ASSISTANT -- ONE-CLICK SETUP
echo ======================================================================
echo.

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not added to PATH.
    echo Please install Python 3.10+ from python.org and check "Add Python to PATH".
    pause
    exit /b 1
)

echo [1/3] Installing Python dependencies...
python -m pip install -r requirements.txt

if not exist .env (
    echo [2/3] Creating .env file from template...
    copy .env.example .env
    echo [INFO] Created .env file. Please edit .env with your API keys and credentials.
) else (
    echo [2/3] Found existing .env file.
)

echo [3/3] Setup completed successfully!
echo.
echo ======================================================================
echo   To launch Sentinel, double-click run.bat or run "python main.py"
echo ======================================================================
pause
