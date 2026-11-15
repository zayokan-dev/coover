@echo off
setlocal
cd /d "%~dp0"

where py.exe >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 goto run_with_launcher
)

where python.exe >nul 2>nul
if errorlevel 1 goto python_missing
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto python_too_old
python "%~dp0server.py"
goto report_exit

:run_with_launcher
py -3 "%~dp0server.py"
goto report_exit

:python_missing
echo Python 3.10 oder neuer wurde nicht gefunden.
echo Installiere Python von https://www.python.org/downloads/ und starte diese Datei erneut.
pause
exit /b 1

:python_too_old
echo Python 3.10 oder neuer wurde nicht gefunden.
echo Aktualisiere Python und starte diese Datei erneut.
pause
exit /b 1

:report_exit
if errorlevel 1 (
    echo.
    echo Das Dashboard konnte nicht gestartet werden. Die Fehlermeldung steht oben.
    pause
)
