@echo off
rem One-step setup for Windows: double-click this. It installs everything
rem Ground Station needs on a new machine, then adds its icon to the Start menu
rem and the Desktop. It needs an internet connection this once; what it
rem installs runs without one afterwards.
rem
rem The machine needs neither git nor Python: uv fetches a Python of its own,
rem and the folder can come from a GitHub download or a USB stick as well as a
rem clone. Safe to run again -- after an update, or after moving the folder --
rem since it only fetches what is missing.
rem
rem No labels or goto on purpose: they break when a download has turned this
rem file's line endings into the Unix kind.
setlocal
cd /d "%~dp0"

rem The Python a new .venv is made with: the one Ground Station is developed
rem on. Left to itself uv takes the newest there is, which some packages may
rem not support yet. A .venv that already works keeps the Python it has; one
rem copied over from another machine doesn't work here and is made again.
set "NEW_VENV_PYTHON=3.14"

rem Where the GPU build of PyTorch comes from on a machine with an NVIDIA GPU.
rem Blackwell cards (RTX 50 series, RTX PRO) need CUDA 12.8 or newer.
set "TORCH_GPU_INDEX=https://download.pytorch.org/whl/cu128"

rem Where uv's installer puts it. A window opened before uv was installed
rem doesn't have this on its PATH yet.
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
set "PY=.venv\Scripts\python.exe"

echo.
echo == 1/4  uv, the tool that installs everything else
where uv >nul 2>nul
if errorlevel 1 powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
where uv >nul 2>nul
if errorlevel 1 (
    echo.
    echo Setup stopped: uv could not be installed. Check the internet connection and run this again.
    pause
    exit /b 1
)
uv --version

echo.
echo == 2/4  Python and the packages Ground Station uses. The first time is a large download.
"%PY%" -c "" >nul 2>nul
if errorlevel 1 (
    uv sync --all-groups --python %NEW_VENV_PYTHON%
) else (
    uv sync --all-groups
)
if errorlevel 1 (
    echo.
    echo Setup stopped: the packages could not be installed -- the lines above say why.
    echo Check the internet connection and run this again.
    pause
    exit /b 1
)

rem PyTorch from the usual place is the processor-only build on Windows. On a
rem machine with a working NVIDIA GPU, swap in the GPU build, unless it is
rem already there. A machine that only annotates doesn't need it.
set "HAS_NVIDIA="
set "HAS_GPU_BUILD="
where nvidia-smi >nul 2>nul
if not errorlevel 1 nvidia-smi >nul 2>nul
if not errorlevel 1 set "HAS_NVIDIA=1"
if defined HAS_NVIDIA "%PY%" -c "import sys, torch; sys.exit(0 if torch.version.cuda else 1)" >nul 2>nul
if defined HAS_NVIDIA if not errorlevel 1 set "HAS_GPU_BUILD=1"
if defined HAS_NVIDIA if not defined HAS_GPU_BUILD (
    echo.
    echo This machine has an NVIDIA GPU: swapping in the GPU build of PyTorch, a few GB more...
    uv pip install --reinstall-package torch --reinstall-package torchvision torch torchvision --index-url %TORCH_GPU_INDEX%
    if errorlevel 1 echo Could not install the GPU build. Ground Station still works; the image models will use the processor instead.
)

echo.
echo == 3/4  The Ground Station icon
"%PY%" -m scraper.cli install-app
if errorlevel 1 (
    echo.
    echo Setup stopped: the icon could not be added -- the lines above say why.
    echo Everything else is installed: launch_gui.bat starts Ground Station without it.
    pause
    exit /b 1
)

echo.
echo == 4/4  Check
"%PY%" -c "from scraper.doctor import check_gpu; print('The image models will run on:', check_gpu().detail)"
if errorlevel 1 echo Could not check which processor the image models will use. The Doctor page in the app has the details.

echo.
echo Done.
echo Open Ground Station from the Start menu or the Desktop.
echo Scraping needs credentials, which go in on the app's API Keys page. Annotating needs none.
echo.
pause
