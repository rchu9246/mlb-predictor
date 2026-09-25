@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   MLB Cumulative Accuracy Stats
echo ============================================
echo.

python mlb_predictor.py --stats

echo.
pause
