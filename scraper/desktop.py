"""Ground Station as an app: an icon you click instead of a command you type.

Three pieces:

- `launch()` is what the icon runs. If Ground Station is already up it just
  opens its window; otherwise it starts the server in the background (no
  terminal window, output in logs/app.log), waits until it answers, then opens
  the window. If the server can't start, it opens a page saying why. A
  server that was started before the code was last changed is replaced first
  (unless it is in the middle of a job): it would serve today's pages with
  yesterday's Python.
- `open_window()` is that window: the same pages, in a window of their own
  rather than a browser tab. The window only looks at the server. Closing it
  leaves the server, and any job it is running, going; opening the app again
  comes back to it. A machine that can't show a window gets the browser.
- `install()` / `remove()` put the icon where each system keeps its apps:
  an .app in ~/Applications on macOS, Start menu and Desktop shortcuts on
  Windows, an applications-menu entry on Linux.
- `draw_icon()` draws the icon itself, so no image file has to be shipped.

The app is a pointer to this project folder and its .venv, not a copy of
them: move or rename the folder and the icon has to be installed again.
"""

import html
import json
import os
import plistlib
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

APP_NAME = "Ground Station"
APP_ID = "ground-station"
DEFAULT_PORT = 8000
PROJECT_DIR = Path(__file__).resolve().parent.parent
LOG_PATH = PROJECT_DIR / "logs" / "app.log"
FAILURE_PAGE = PROJECT_DIR / "logs" / "app-could-not-start.html"
MAX_LOG_BYTES = 2_000_000
WINDOW_DIR = PROJECT_DIR / "logs" / "window"  # the window's icon and what its pages remember; gitignored with logs/
WINDOW_SIZE = (1440, 920)
WINDOW_MIN_SIZE = (960, 640)

# The same colours as the pages (style.css): ink, paper, red.
_INK = (26, 26, 24, 255)
_PAPER = (232, 227, 211, 255)
_RED = (194, 62, 15, 255)


# ---- Is it running? ----


def local_url(port: int = DEFAULT_PORT) -> str:
    return f"http://127.0.0.1:{port}/"


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never through a proxy: it's this machine


def _ping(url: str, timeout: float = 1.0) -> dict | None:
    """What the Ground Station server at url says about itself, or None when
    nothing answers there or something else has the port."""
    try:
        with _opener().open(url.rstrip("/") + "/api/ping", timeout=timeout) as response:
            answer = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return answer if isinstance(answer, dict) and answer.get("app") == APP_ID else None


def is_running(url: str, timeout: float = 1.0) -> bool:
    """True when a Ground Station server answers at url -- not just anything
    that happens to be listening on that port."""
    return _ping(url, timeout) is not None


def code_changed_at(package_dir: Path = PROJECT_DIR / "scraper") -> float:
    """When the newest Python file of the app was last changed. Pages, scripts
    and styles are read from disk on every request, so only Python can go
    stale in a running server."""
    changed = 0.0
    for path in package_dir.rglob("*.py"):
        try:
            changed = max(changed, path.stat().st_mtime)
        except OSError:  # removed while we were looking
            continue
    return changed


def runs_old_code(url: str) -> bool:
    """True when the server at url was started before the code on disk was
    last changed (an update was pulled, or a file was edited, while it kept
    running). It would serve the new pages with the old Python, and whatever
    the new pages ask for that the old Python doesn't have comes back 404. A
    server too old to say when it started counts as old."""
    answer = _ping(url)
    if answer is None:
        return False
    started = answer.get("started")
    return not isinstance(started, (int, float)) or started < code_changed_at()


def stop_server(url: str, wait_seconds: float = 15.0) -> bool:
    """Asks the server at url to quit, the way its Quit button does, and
    waits for it to go. False when it is still there afterwards -- it refuses
    while a job is running, and a job is never cut short for this."""
    request = urllib.request.Request(
        url.rstrip("/") + "/api/quit",
        data=json.dumps({"force": False}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _opener().open(request, timeout=15.0):
            pass
    except urllib.error.HTTPError:  # 409: a job is running
        return False
    except OSError:
        pass  # it may have gone before it could answer; the wait below settles it
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if not is_running(url):
            return True
        time.sleep(0.25)
    return not is_running(url)


# ---- Launch ----


def _console_python(python: Path) -> Path:
    """pythonw.exe (what a Windows shortcut runs, so no console flashes up)
    has no output streams. The server is started with python.exe instead,
    hidden, so its output can go to the log file."""
    if python.name.lower() == "pythonw.exe":
        sibling = python.with_name("python.exe")
        if sibling.exists():
            return sibling
    return python


def start_server(port: int = DEFAULT_PORT, *, log_path: Path = LOG_PATH, python: Path | None = None):
    """Starts the server in the background, detached from whatever started
    it, with its output appended to log_path. Returns the process."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    start_over = log_path.exists() and log_path.stat().st_size > MAX_LOG_BYTES
    command = [
        str(_console_python(python or Path(sys.executable))),
        "-m",
        "scraper.cli",
        "serve",
        "--no-browser",
        "--port",
        str(port),
    ]
    detached = (
        {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    with open(log_path, "w" if start_over else "a", encoding="utf-8") as log:
        log.write(f"\n--- {APP_NAME} started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        log.flush()
        return subprocess.Popen(
            command,
            cwd=PROJECT_DIR,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            **detached,
        )


def _wait_until_running(url: str, process, wait_seconds: float) -> bool:
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if is_running(url):
            return True
        if process.poll() is not None:  # it gave up; no point waiting out the clock
            return False
        time.sleep(0.25)
    return is_running(url)


def write_failure_page(log_path: Path = LOG_PATH, page_path: Path = FAILURE_PAGE) -> Path:
    """A page that says the app didn't start and shows the end of its log --
    there's no terminal window to read the error from."""
    try:
        tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-40:])
    except OSError:
        tail = "(no log was written)"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text(
        "<!doctype html><meta charset='utf-8'><title>Ground Station could not start</title>"
        "<body style='font-family: system-ui, sans-serif; max-width: 60rem; margin: 3rem auto; padding: 0 1rem;"
        " background: #e8e3d3; color: #1a1a18'>"
        "<h1>Ground Station could not start</h1>"
        "<p>The last lines of its log are below. If they mention an address already in use, another program"
        " has the port. If they mention a missing module, run <code>uv sync --all-groups</code> in the project"
        " folder.</p>"
        f"<p>Full log: <code>{html.escape(str(log_path))}</code></p>"
        "<pre style='background: #1a1a18; color: #e8e3d3; padding: 1rem; overflow-x: auto; white-space: pre-wrap'>"
        f"{html.escape(tail)}</pre></body>",
        encoding="utf-8",
    )
    return page_path


def launch(port: int = DEFAULT_PORT, *, wait_seconds: float = 60.0, open_url=None) -> bool:
    """What the app icon does. Returns True when Ground Station was opened,
    False when it could not be started. `open_url` is what shows a page:
    `show` unless told otherwise, which doesn't come back until its window
    is closed."""
    open_url = open_url or show
    url = local_url(port)
    running = is_running(url)
    if running and runs_old_code(url):
        running = not stop_server(url)  # left running if it's busy; its pages say it needs a restart
    if not running:
        process = start_server(port)
        if not _wait_until_running(url, process, wait_seconds):
            open_url(write_failure_page().as_uri())
            return False
    open_url(url)
    return True


# ---- The window ----


def _window_icon(folder: Path = WINDOW_DIR) -> Path:
    """The icon as a file, for the window's title bar and its taskbar or Dock
    entry. Drawn the first time and kept."""
    windows = os.name == "nt"
    path = folder / ("icon.ico" if windows else "icon.png")
    if not path.exists():
        folder.mkdir(parents=True, exist_ok=True)
        if windows:
            draw_icon(256, margin=0.02).save(path, sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
        else:
            draw_icon(512, margin=0.1 if sys.platform == "darwin" else 0.02).save(path)
    return path


def _allow_page_full_screen(window) -> None:
    """macOS: lets a page take over the screen (the Demo page's Full screen
    button). The web view there has that switched off until asked, and it has
    to be asked before the page loads. Windows and Linux don't need it."""
    if sys.platform != "darwin":
        return
    try:
        from webview.platforms.cocoa import BrowserView

        BrowserView.instances[window.uid].webview.configuration().preferences().setElementFullscreenEnabled_(True)
    except Exception:  # an older macOS or a changed library: the page hides its Full screen button
        pass


def _name_the_mac_app() -> None:
    """macOS: the program is Python, so the menu bar would say "Python" and
    offer to "Quit Python". This has it say Ground Station."""
    if sys.platform != "darwin":
        return
    try:
        from Foundation import NSBundle

        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = APP_NAME
    except Exception:  # only the name in the menu bar is at stake
        pass


def _full_screen_follower(window, follows: bool = os.name == "nt"):
    """What a page calls (`pywebview.api.full_screen`) when something on it
    goes full screen or comes back. On Windows the web view only fills its
    own window, so the window has to follow it to cover the screen; macOS and
    Linux do that themselves, and there this does nothing."""
    state = {"on": False}

    def full_screen(on) -> None:
        if follows and bool(on) != state["on"]:
            state["on"] = bool(on)
            window.toggle_fullscreen()

    return full_screen


def _window_closer(window, delay: float = 0.3):
    """What a page calls (`pywebview.api.close_window`) once Quit has stopped
    the server: there is nothing left for the window to show.

    The window goes a moment after the call, not during it. The library
    answers the page on the calling thread once this returns, and an answer
    sent to a window that has already gone never arrives: that thread would
    wait for ever and keep the process (and its Dock icon) alive."""

    def close_window() -> None:
        timer = threading.Timer(delay, window.destroy)
        timer.daemon = True
        timer.start()

    return close_window


def _note(message: str, log_path: Path = LOG_PATH) -> None:
    """A line in the app's log: an icon has no terminal to print to."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def open_window(url: str) -> bool:
    """Shows url in a Ground Station window and doesn't come back until that
    window is closed. False when this machine can't show one -- the window
    library isn't installed, or the system has no web engine for it (Linux
    without the GTK WebKit packages) -- so the caller can use the browser.

    Must be called from the program's main thread."""
    try:
        import webview
    except ImportError:
        return False

    shown: list[bool] = []
    try:
        if os.name == "nt":  # its own taskbar entry and icon, not Python's
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("local.ground-station.app")
        _name_the_mac_app()
        webview.settings["ALLOW_DOWNLOADS"] = True  # the ledger download
        window = webview.create_window(
            APP_NAME,
            url,
            width=WINDOW_SIZE[0],
            height=WINDOW_SIZE[1],
            min_size=WINDOW_MIN_SIZE,
            text_select=True,  # off unless asked for; paths and log lines get copied
        )
        window.events.before_show += _allow_page_full_screen
        window.events.shown += lambda: shown.append(True)
        window.expose(_full_screen_follower(window), _window_closer(window))
        # Not private: Review's layout and the annotator's name are remembered between runs.
        webview.start(private_mode=False, storage_path=str(WINDOW_DIR), icon=str(_window_icon()))
    except Exception as error:
        _note(f"The {APP_NAME} window stopped: {error!r}")
        return bool(shown)  # it was open and has gone: nothing to hand to the browser
    return True


def show(url: str) -> None:
    """Opens a page for whoever clicked the icon: in a window of its own
    where this machine can show one, in the browser where it can't."""
    if not open_window(url):
        webbrowser.open(url)


# ---- The icon ----


def draw_icon(size: int = 1024, *, margin: float = 0.1):
    """The Ground Station mark -- the sidebar's satellite -- on a dark tile.
    `margin` is the empty border around the tile as a share of the size:
    macOS icons leave about a tenth, Windows and Linux ones fill the square."""
    from PIL import Image, ImageDraw

    scale = 4  # drawn large and shrunk, since ImageDraw doesn't smooth edges
    canvas = 1024 * scale
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    inset = canvas * margin
    tile = canvas - 2 * inset
    draw.rounded_rectangle((inset, inset, canvas - inset, canvas - inset), radius=tile * 0.225, fill=_INK)

    # The mark is laid out on the same 24-unit grid as the sidebar icon.
    unit = tile * 0.82 / 24
    left = inset + (tile - 24 * unit) / 2
    top = inset + (tile - 24 * unit) / 2 - 0.4 * unit

    def at(x: float, y: float) -> tuple[float, float]:
        return (left + x * unit, top + y * unit)

    def box(x0: float, y0: float, x1: float, y1: float, radius: float, fill) -> None:
        draw.rounded_rectangle((*at(x0, y0), *at(x1, y1)), radius=radius * unit, fill=fill)

    def line(x0: float, y0: float, x1: float, y1: float, fill, width: float = 1.0) -> None:
        stroke = width * unit
        draw.line((*at(x0, y0), *at(x1, y1)), fill=fill, width=round(stroke))
        for x, y in (at(x0, y0), at(x1, y1)):  # round ends
            draw.ellipse((x - stroke / 2, y - stroke / 2, x + stroke / 2, y + stroke / 2), fill=fill)

    for strut_y in (10.6, 13.4):  # the arms holding the panels
        line(4.5, strut_y, 19.5, strut_y, _PAPER, 0.8)
    for panel_left in (0.6, 18.6):  # solar panels, each split in two cells
        box(panel_left, 8.2, panel_left + 4.8, 15.8, 0.5, _PAPER)
        draw.line((*at(panel_left, 12), *at(panel_left + 4.8, 12)), fill=_INK, width=round(0.45 * unit))
    box(8.4, 8.4, 15.6, 15.6, 1.1, _PAPER)  # the body
    line(12, 8.4, 12, 5.2, _PAPER, 0.9)  # antenna
    dot_x, dot_y = at(12, 4.2)
    draw.ellipse((dot_x - 1.3 * unit, dot_y - 1.3 * unit, dot_x + 1.3 * unit, dot_y + 1.3 * unit), fill=_RED)
    line(7.2, 17.8, 3.6, 21.4, _RED, 1.1)  # signal, down to the ground
    line(10.6, 19.4, 7.8, 22.2, _RED, 1.1)

    return image.resize((size, size), Image.LANCZOS)


# ---- Install ----


def _launch_arguments(port: int) -> list[str]:
    arguments = ["-m", "scraper.cli", "launch"]
    if port != DEFAULT_PORT:
        arguments += ["--port", str(port)]
    return arguments


def install_mac(apps_dir: Path, *, python: Path, project: Path = PROJECT_DIR, port: int = DEFAULT_PORT) -> Path:
    """Builds "Ground Station.app": a small bundle whose program is a shell
    script that runs `launch` from the project folder."""
    bundle = apps_dir / f"{APP_NAME}.app"
    contents = bundle / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    (contents / "Resources").mkdir(parents=True, exist_ok=True)

    (contents / "Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleName": APP_NAME,
                "CFBundleDisplayName": APP_NAME,
                "CFBundleIdentifier": "local.ground-station.launcher",
                "CFBundleExecutable": APP_ID,
                "CFBundleIconFile": "icon",
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": "3.0",
                "CFBundleVersion": "3.0",
                "NSHighResolutionCapable": True,
            }
        )
    )

    missing = (
        f"{APP_NAME} was set up for the folder {project}, which has moved or lost its .venv. "
        "Run install-app again from where the project is now."
    )
    alert = f"display alert {json.dumps(APP_NAME)} message {json.dumps(missing)}"
    program = contents / "MacOS" / APP_ID
    program.write_text(
        "#!/bin/sh\n"
        f"# Made by `python -m scraper.cli install-app`. Runs {APP_NAME} from its project folder.\n"
        f"if [ ! -x {shlex.quote(str(python))} ]; then\n"
        f"    osascript -e {shlex.quote(alert)}\n"
        "    exit 1\n"
        "fi\n"
        f"cd {shlex.quote(str(project))} || exit 1\n"
        f"exec {shlex.quote(str(python))} {' '.join(_launch_arguments(port))}\n",
        encoding="utf-8",
    )
    program.chmod(0o755)

    draw_icon(1024, margin=0.1).save(contents / "Resources" / "icon.icns")
    os.utime(bundle)  # nudges Finder into showing the new icon rather than a cached one
    return bundle


def linux_paths(data_home: Path) -> tuple[Path, Path]:
    return data_home / "applications" / f"{APP_ID}.desktop", data_home / "icons" / f"{APP_ID}.png"


def _desktop_exec_quote(value: str) -> str:
    """Quoting for a .desktop file's Exec line (the Desktop Entry spec's own
    rules, close to but not the same as a shell's)."""
    for character in ("\\", '"', "`", "$"):
        value = value.replace(character, "\\" + character)
    return f'"{value}"'


def install_linux(data_home: Path, *, python: Path, project: Path = PROJECT_DIR, port: int = DEFAULT_PORT) -> Path:
    """Adds Ground Station to the applications menu."""
    entry, icon = linux_paths(data_home)
    entry.parent.mkdir(parents=True, exist_ok=True)
    icon.parent.mkdir(parents=True, exist_ok=True)
    draw_icon(256, margin=0.02).save(icon)
    entry.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=Satellite image pipeline\n"
        f"Exec={_desktop_exec_quote(str(python))} {' '.join(_launch_arguments(port))}\n"
        f"Path={project}\n"
        f"Icon={icon}\n"
        "Terminal=false\n"
        "Categories=Science;Development;\n",
        encoding="utf-8",
    )
    entry.chmod(0o755)
    return entry


def windows_icon_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "GroundStation" / f"{APP_ID}.ico"


# PowerShell is the one thing every Windows machine has that can write a
# shortcut (.lnk). Values come in through the environment, never pasted into
# the script, so a folder name with quotes or spaces can't break it. The
# Desktop and Start menu folders are asked for rather than assumed -- OneDrive
# moves the Desktop.
_WINDOWS_FOLDERS = "@([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))"
_WINDOWS_INSTALL_SCRIPT = (
    "$shell = New-Object -ComObject WScript.Shell\n"
    f"foreach ($folder in {_WINDOWS_FOLDERS}) {{\n"
    "  $link = $shell.CreateShortcut((Join-Path $folder ($env:GS_NAME + '.lnk')))\n"
    "  $link.TargetPath = $env:GS_TARGET\n"
    "  $link.Arguments = $env:GS_ARGUMENTS\n"
    "  $link.WorkingDirectory = $env:GS_PROJECT\n"
    "  $link.IconLocation = $env:GS_ICON\n"
    "  $link.Description = 'Satellite image pipeline'\n"
    "  $link.Save()\n"
    "  Write-Output $link.FullName\n"
    "}\n"
)
_WINDOWS_REMOVE_SCRIPT = (
    f"foreach ($folder in {_WINDOWS_FOLDERS}) {{\n"
    "  $path = Join-Path $folder ($env:GS_NAME + '.lnk')\n"
    "  if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path; Write-Output $path }\n"
    "}\n"
)


def _run_powershell(script: str, variables: dict[str, str]) -> list[str]:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        env={**os.environ, **variables},
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def install_windows(
    *, python: Path, project: Path = PROJECT_DIR, port: int = DEFAULT_PORT, run=_run_powershell
) -> list[str]:
    """Puts a Ground Station shortcut on the Desktop and in the Start menu."""
    icon = windows_icon_path()
    icon.parent.mkdir(parents=True, exist_ok=True)
    draw_icon(256, margin=0.02).save(icon, sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    windowless = python.with_name("pythonw.exe")  # no console window flashing up on each click
    return run(
        _WINDOWS_INSTALL_SCRIPT,
        {
            "GS_NAME": APP_NAME,
            "GS_TARGET": str(windowless if windowless.exists() else python),
            "GS_ARGUMENTS": " ".join(_launch_arguments(port)),
            "GS_PROJECT": str(project),
            "GS_ICON": str(icon),
        },
    )


def _mac_apps_dir() -> Path:
    return Path.home() / "Applications"


def _linux_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def install(port: int = DEFAULT_PORT) -> list[str]:
    """Installs the icon for this machine. Returns what was made."""
    python = Path(sys.executable)  # the .venv's own python, not what it links to
    if sys.platform == "darwin":
        return [str(install_mac(_mac_apps_dir(), python=python, port=port))]
    if os.name == "nt":
        return install_windows(python=python, port=port)
    return [str(install_linux(_linux_data_home(), python=python, port=port))]


def remove() -> list[str]:
    """Takes the icon away again -- only what install() made. The project,
    its images and its database are not touched. Returns what was removed."""
    import shutil

    removed: list[str] = []
    if sys.platform == "darwin":
        bundle = _mac_apps_dir() / f"{APP_NAME}.app"
        if bundle.is_dir():
            shutil.rmtree(bundle)
            removed.append(str(bundle))
    elif os.name == "nt":
        removed += _run_powershell(_WINDOWS_REMOVE_SCRIPT, {"GS_NAME": APP_NAME})
        icon = windows_icon_path()
        if icon.exists():
            icon.unlink()
            removed.append(str(icon))
    else:
        for path in linux_paths(_linux_data_home()):
            if path.exists():
                path.unlink()
                removed.append(str(path))
    return removed
