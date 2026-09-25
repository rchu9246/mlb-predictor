@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [%date% %time%] Starting daily update >> update_log.txt

python mlb_predictor.py --live --html --no-open >> update_log.txt 2>&1

if errorlevel 1 (
    echo [%date% %time%] FAILED - see messages above >> update_log.txt
) else (
    echo [%date% %time%] SUCCESS - report.html updated >> update_log.txt
)

echo. >> update_log.txt

REM Also verify yesterday's predictions (silent, log-only)
python mlb_predictor.py --verify --html --no-open >> update_log.txt 2>&1

echo ================================ >> update_log.txt
