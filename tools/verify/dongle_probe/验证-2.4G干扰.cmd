@echo off
chcp 65001 >nul
cd /d "%~dp0"
python "interference_test.py" %*
echo.
echo ============================================================
echo  Done. Window kept open, please scroll up to review the hints.
echo ============================================================
pause
