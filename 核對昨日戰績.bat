@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   MLB Verify Yesterday's Predictions
echo ============================================
echo.

python mlb_predictor.py --verify --html

if errorlevel 1 (
    echo.
    echo [ERROR] Something went wrong. See messages above.
    echo Please screenshot this window and send it for help.
    pause
) else (
    echo.
    echo Done! The verification report should have opened in your browser.
    timeout /t 5
)
