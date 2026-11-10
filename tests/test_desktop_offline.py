"""Ground Station as an app: the launcher, the Quit button and install-app."""

import io
import os
import plistlib
import sys
import threading
import urllib.error
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import scraper.web.app as web_app
from scraper import cli, desktop

runner = CliRunner()


@pytest.fixture
def local_client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    web_app.DB_PATH = tmp_path / "test.db"
    web_app._jobs.clear()
    with TestClient(web_app.app, client=("127.0.0.1", 12345)) as client:
        yield client
    web_app._jobs.clear()


class _FakeProcess:
    def __init__(self, exit_code=None):
        self.exit_code = exit_code

    def poll(self):
        return self.exit_code


# ---- launch ----


def test_launch_only_opens_the_browser_when_it_is_already_running(monkeypatch):
    monkeypatch.setattr(desktop, "is_running", lambda url: True)
    monkeypatch.setattr(desktop, "runs_old_code", lambda url: False)
    monkeypatch.setattr(desktop, "start_server", lambda port: pytest.fail("started a second server"))
    opened = []

    assert desktop.launch(8123, open_url=opened.append) is True
    assert opened == ["http://127.0.0.1:8123/"]


def test_launch_starts_the_server_and_waits_for_it_before_opening_the_browser(monkeypatch):
    answers = iter([False, False, True])  # not running, still starting, up
    monkeypatch.setattr(desktop, "is_running", lambda url: next(answers))
    monkeypatch.setattr(desktop.time, "sleep", lambda seconds: None)
    started = []
    monkeypatch.setattr(desktop, "start_server", lambda port: started.append(port) or _FakeProcess())
    opened = []

    assert desktop.launch(8123, open_url=opened.append) is True
    assert started == [8123]
    assert opened == ["http://127.0.0.1:8123/"]


def test_launch_shows_the_log_when_the_server_cannot_start(tmp_path, monkeypatch):
    log = tmp_path / "logs" / "app.log"
    log.parent.mkdir()
    log.write_text("starting\n[Errno 48] address already in use <b>\n")
    monkeypatch.setattr(desktop, "is_running", lambda url: False)
    monkeypatch.setattr(desktop, "start_server", lambda port: _FakeProcess(exit_code=1))
    opened = []

    page = desktop.write_failure_page(log, tmp_path / "logs" / "failed.html")
    monkeypatch.setattr(desktop, "write_failure_page", lambda: page)

    assert desktop.launch(8123, open_url=opened.append) is False
    assert opened == [page.as_uri()]  # the page explaining why, not a dead address
    text = page.read_text()
    assert "address already in use" in text
    assert "&lt;b&gt;" in text  # log text is shown, never run as markup


def test_launch_replaces_a_server_that_runs_old_code(monkeypatch):
    # The pages come from disk but the Python doesn't: a server started
    # before an update answers the new pages' requests with 404.
    state = {"running": True}
    monkeypatch.setattr(desktop, "is_running", lambda url: state["running"])
    monkeypatch.setattr(desktop, "runs_old_code", lambda url: True)
    steps = []

    def stop(url):
        steps.append("stopped the old one")
        state["running"] = False
        return True

    def start(port):
        steps.append("started a new one")
        state["running"] = True
        return _FakeProcess()

    monkeypatch.setattr(desktop, "stop_server", stop)
    monkeypatch.setattr(desktop, "start_server", start)
    opened = []

    assert desktop.launch(8123, open_url=opened.append) is True
    assert steps == ["stopped the old one", "started a new one"]
    assert opened == ["http://127.0.0.1:8123/"]


def test_launch_leaves_an_old_server_alone_while_it_is_busy(monkeypatch):
    monkeypatch.setattr(desktop, "is_running", lambda url: True)
    monkeypatch.setattr(desktop, "runs_old_code", lambda url: True)
    monkeypatch.setattr(desktop, "stop_server", lambda url: False)  # a job is running
    monkeypatch.setattr(desktop, "start_server", lambda port: pytest.fail("started a second server"))
    opened = []

    assert desktop.launch(8123, open_url=opened.append) is True
    assert opened == ["http://127.0.0.1:8123/"]


@pytest.mark.parametrize(
    "answer, old",
    [
        ({"app": desktop.APP_ID, "started": 100.0}, True),  # started, then the code changed
        ({"app": desktop.APP_ID, "started": 300.0}, False),  # started after the last change
        ({"app": desktop.APP_ID}, True),  # too old to say when it started
        (None, False),  # nothing there to replace
    ],
)
def test_a_server_started_before_the_code_last_changed_runs_old_code(monkeypatch, answer, old):
    monkeypatch.setattr(desktop, "_ping", lambda url: answer)
    monkeypatch.setattr(desktop, "code_changed_at", lambda: 200.0)

    assert desktop.runs_old_code("http://127.0.0.1:8123/") is old


def test_code_changed_at_is_the_newest_python_file(tmp_path):
    (tmp_path / "web").mkdir()
    older, newer, page = tmp_path / "a.py", tmp_path / "web" / "b.py", tmp_path / "web" / "page.html"
    for path, changed in ((older, 1_000), (newer, 2_000), (page, 3_000)):
        path.write_text("", encoding="utf-8")
        os.utime(path, (changed, changed))

    assert desktop.code_changed_at(tmp_path) == 2_000  # pages are read from disk each time; they can't go stale
    assert desktop.code_changed_at(tmp_path / "missing") == 0.0


class _Refusing:
    """Stands in for a server that answers Quit with 409: a job is running."""

    def open(self, request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 409, "Conflict", None, io.BytesIO(b""))


def test_stop_server_never_cuts_a_job_short(monkeypatch):
    monkeypatch.setattr(desktop, "_opener", lambda: _Refusing())
    monkeypatch.setattr(desktop, "is_running", lambda url: pytest.fail("waited for a server that said no"))

    assert desktop.stop_server("http://127.0.0.1:8123/") is False


class _Quitting:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        return io.BytesIO(b'{"status": "quitting"}')


def test_stop_server_asks_without_forcing_and_waits_until_it_has_gone(monkeypatch):
    quitting = _Quitting()
    monkeypatch.setattr(desktop, "_opener", lambda: quitting)
    answers = iter([True, True, False])  # still shutting down, then gone
    monkeypatch.setattr(desktop, "is_running", lambda url: next(answers))
    monkeypatch.setattr(desktop.time, "sleep", lambda seconds: None)

    assert desktop.stop_server("http://127.0.0.1:8123/") is True
    (request,) = quitting.requests
    assert request.full_url == "http://127.0.0.1:8123/api/quit"
    assert request.data == b'{"force": false}'


def test_is_running_is_false_when_nothing_answers():
    assert desktop.is_running("http://127.0.0.1:9/", timeout=0.2) is False


def test_the_server_says_it_is_ground_station_and_when_it_started(local_client):
    assert local_client.get("/api/ping").json() == {"app": desktop.APP_ID, "started": web_app.STARTED_AT}


def test_pages_say_when_the_server_needs_a_restart(local_client, monkeypatch):
    assert 'id="restart-notice"' not in local_client.get("/review").text

    monkeypatch.setattr(web_app.desktop, "code_changed_at", lambda: web_app.STARTED_AT + 60)

    assert 'id="restart-notice"' in local_client.get("/review").text


def test_serve_opens_the_running_one_instead_of_starting_a_second(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.desktop, "is_running", lambda url: True)
    monkeypatch.setattr("uvicorn.Server.run", lambda *a, **k: pytest.fail("started a second server"))
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

    result = runner.invoke(cli.app, ["--db-path", str(tmp_path / "t.db"), "serve", "--port", "8123"])

    assert result.exit_code == 0
    assert "already running" in result.stdout
    assert opened == ["http://127.0.0.1:8123/"]


def test_the_launch_command_reports_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.desktop, "launch", lambda port: False)
    result = runner.invoke(cli.app, ["--db-path", str(tmp_path / "t.db"), "launch"])
    assert result.exit_code == 1


# ---- Quit ----


def test_quit_stops_the_server(local_client, monkeypatch):
    stopped = threading.Event()
    monkeypatch.setattr(web_app, "_stop_server", stopped.set)
    monkeypatch.setattr(web_app, "QUIT_DELAY_SECONDS", 0)

    resp = local_client.post("/api/quit", json={})

    assert resp.status_code == 200
    assert stopped.wait(timeout=2)


def test_quit_is_refused_from_another_machine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    web_app.DB_PATH = tmp_path / "test.db"
    monkeypatch.setattr(web_app, "_stop_server", lambda: pytest.fail("stopped by a remote request"))
    with TestClient(web_app.app) as remote_client:
        assert remote_client.post("/api/quit", json={"force": True}).status_code == 403


def test_quit_waits_for_a_running_job_unless_told_to_stop_it(local_client, monkeypatch):
    stopped = threading.Event()
    monkeypatch.setattr(web_app, "_stop_server", stopped.set)
    monkeypatch.setattr(web_app, "QUIT_DELAY_SECONDS", 0)
    job_id, entry = web_app._new_job("Scrape", "scrape")
    job_stopped = threading.Event()

    def fake_job():
        entry["cancel"].wait(timeout=5)
        entry["status"] = "done"
        job_stopped.set()

    threading.Thread(target=fake_job, daemon=True).start()

    refused = local_client.post("/api/quit", json={})
    assert refused.status_code == 409
    assert "Scrape" in refused.json()["detail"]
    assert not stopped.is_set() and not job_stopped.is_set()

    assert local_client.post("/api/quit", json={"force": True}).status_code == 200
    assert job_stopped.is_set()  # the job was stopped cleanly first
    assert stopped.wait(timeout=2)


def test_the_quit_button_and_icon_are_on_every_page(local_client):
    page = local_client.get("/status").text
    assert 'id="quit-btn"' in page
    assert 'href="/favicon.png"' in page
    icon = local_client.get("/favicon.png")
    assert icon.status_code == 200
    assert icon.content.startswith(b"\x89PNG")


# ---- install-app ----


def test_the_icon_is_drawn_at_the_size_asked_for():
    icon = desktop.draw_icon(256, margin=0.02)
    assert icon.size == (256, 256)
    assert icon.getpixel((0, 0))[3] == 0  # corners are see-through
    assert icon.getpixel((128, 128)) == desktop._PAPER  # the satellite's body, in the middle
    assert icon.getpixel((128, 230)) == desktop._INK  # on the dark tile


def test_the_mac_app_runs_launch_from_the_project_folder(tmp_path):
    project = tmp_path / "My Project's folder"
    python = project / ".venv" / "bin" / "python"

    bundle = desktop.install_mac(tmp_path / "Applications", python=python, project=project, port=8123)

    assert bundle.name == "Ground Station.app"
    info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
    program = bundle / "Contents" / "MacOS" / info["CFBundleExecutable"]
    script = program.read_text()
    assert "-m scraper.cli launch --port 8123" in script
    assert "My Project" in script
    if sys.platform != "win32":
        import subprocess

        assert program.stat().st_mode & 0o111  # it can be run
        assert subprocess.run(["sh", "-n", str(program)]).returncode == 0  # and the awkward folder name is quoted
    assert (bundle / "Contents" / "Resources" / f"{info['CFBundleIconFile']}.icns").stat().st_size > 1000

    desktop.install_mac(tmp_path / "Applications", python=python, project=project)  # installing twice is fine
    assert "--port" not in program.read_text()


def test_the_linux_menu_entry_runs_launch_from_the_project_folder(tmp_path):
    project = tmp_path / "ground station"
    python = project / ".venv" / "bin" / "python"

    entry = desktop.install_linux(tmp_path / "share", python=python, project=project)

    text = entry.read_text()
    assert f'Exec="{python}" -m scraper.cli launch\n' in text
    assert f"Path={project}\n" in text
    assert "Terminal=false" in text
    icon = Path(next(line for line in text.splitlines() if line.startswith("Icon=")).removeprefix("Icon="))
    assert icon.read_bytes().startswith(b"\x89PNG")


def test_the_windows_shortcut_points_at_the_windowless_python(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    scripts = tmp_path / "project" / ".venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "pythonw.exe").write_bytes(b"")
    calls = []

    def fake_powershell(script, variables):
        calls.append((script, variables))
        return ["C:/Users/me/Desktop/Ground Station.lnk"]

    made = desktop.install_windows(python=scripts / "python.exe", project=tmp_path / "project", run=fake_powershell)

    assert made == ["C:/Users/me/Desktop/Ground Station.lnk"]
    script, variables = calls[0]
    assert "CreateShortcut" in script
    assert variables["GS_TARGET"] == str(scripts / "pythonw.exe")
    assert variables["GS_ARGUMENTS"] == "-m scraper.cli launch"
    assert variables["GS_PROJECT"] == str(tmp_path / "project")
    assert Path(variables["GS_ICON"]).read_bytes()[:4] == b"\x00\x00\x01\x00"  # a real .ico file


def test_the_background_server_is_started_with_a_python_that_has_output(tmp_path):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "python.exe").write_bytes(b"")
    assert desktop._console_python(scripts / "pythonw.exe") == scripts / "python.exe"
    assert desktop._console_python(scripts / "python") == scripts / "python"


def test_install_app_command_installs_and_removes(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.desktop, "install", lambda port: ["/Applications/Ground Station.app"])
    monkeypatch.setattr(cli.desktop, "remove", lambda: [])

    installed = runner.invoke(cli.app, ["--db-path", str(tmp_path / "t.db"), "install-app"])
    assert installed.exit_code == 0
    assert "Installed /Applications/Ground Station.app" in installed.stdout

    removed = runner.invoke(cli.app, ["--db-path", str(tmp_path / "t.db"), "install-app", "--remove"])
    assert removed.exit_code == 0
    assert "Nothing to remove" in removed.stdout
