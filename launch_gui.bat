@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo .venv not found -- run "uv sync --all-groups" in this folder first.
    pause
    exit /b 1
)

echo Starting Scraper 3.0 -- your browser will open automatically...
".venv\Scripts\python.exe" -m scraper.cli serve %*

if errorlevel 1 (
    echo.
    echo Scraper 3.0 exited with an error -- see above.
    pause
)
