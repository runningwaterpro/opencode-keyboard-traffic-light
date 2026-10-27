@echo off
chcp 65001 >nul
cd /d "%~dp0"
python "menu.py"
echo.
echo ============================================================
echo  Done. Window kept open, please scroll up to review the result.
echo ============================================================
pause