@echo off
title Sentinel AI Trading Assistant
echo ======================================================================
echo   🛡️ LAUNCHING SENTINEL AI TRADING ASSISTANT...
echo ======================================================================
python main.py
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Sentinel exited with error code %errorlevel%.
    pause
)
