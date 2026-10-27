@echo off
chcp 65001 >nul
cd /d "%~dp0..\.."
python "tools\verify\wired_walkthrough.py" %*
echo.
echo ============================================================
echo  Done. Window kept open, please scroll up to review.
echo ============================================================
pause