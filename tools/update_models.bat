@echo off
setlocal
cd /d "%~dp0\.."
echo ============================================================
echo  spoolwarden-vision: One-Click Model Training Update
echo ============================================================
echo Scanning data\feedback_incoming\ and %%USERPROFILE%%\Downloads\
echo for filament-feedback-*.zip bundles...
echo.
".venv\Scripts\spoolwarden-vision.exe" update-models --min-new-samples 10 %*
echo.
echo Done. Press any key to close...
pause >nul
