@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo .venv not found -- run "uv sync --all-groups" in this folder first.
    pause
    exit /b 1
)

echo Starting Ground Station -- your browser will open automatically...
".venv\Scripts\python.exe" -m scraper.cli serve %*

if errorlevel 1 (
    echo.
    echo Ground Station exited with an error -- see above.
    pause
)
