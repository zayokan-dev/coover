@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0dashboard-steuerung.ps1"
if errorlevel 1 (
    echo.
    echo Die Dashboard-Steuerung wurde mit einem Fehler beendet.
    pause
)
