"""Offline tests for scraper/catalog.py. No real HTTP, no real Space-Track
credentials -- a fake session stands in for requests.Session, so the real
login()/fetch_satellites()/refresh_catalog() code runs exactly as it will
in production, same pattern as tests/test_download_stage_offline.py.
"""

import json

import pytest

from scraper import catalog


class FakeResponse:
    def __init__(self, json_data=None, status_code=200):
        self._json = json_data
        self.status_code = status_code

    def json(self):
        return self._json


SATCAT_RESPONSE = [
    {"NORAD_CAT_ID": "1", "OBJECT_NAME": "STARLINK-1234", "LAUNCH": "2021-07-01"},
    {"NORAD_CAT_ID": "2", "OBJECT_NAME": "COSMOS 2403 (GLONASS)", "LAUNCH": "2005-01-01"},
    {"NORAD_CAT_ID": "3", "OBJECT_NAME": "GOES 16", "LAUNCH": "2016-11-19"},
]


class FakeSession:
    def __init__(self, login_status=200):
        self.login_status = login_status
        self.logged_out = False

    def post(self, url, data=None, **kwargs):
        assert url == catalog.LOGIN_URL
        return FakeResponse(status_code=self.login_status)

    def get(self, url, **kwargs):
        if url == catalog.SATCAT_URL:
            return FakeResponse(json_data=SATCAT_RESPONSE)
        if url == f"{catalog.BASE_URL}/ajaxauth/logout":
            self.logged_out = True
            return FakeResponse(status_code=200)
        raise AssertionError(f"unexpected URL: {url}")


@pytest.fixture
def spacetrack_creds(monkeypatch):
    monkeypatch.setenv("SPACETRACK_USERNAME", "fake-user")
    monkeypatch.setenv("SPACETRACK_PASSWORD", "fake-pass")


def test_login_success(spacetrack_creds):
    assert catalog.login(FakeSession(login_status=200)) is True


def test_login_failure(spacetrack_creds):
    assert catalog.login(FakeSession(login_status=403)) is False


def test_login_raises_clearly_when_credentials_are_missing(monkeypatch):
    monkeypatch.delenv("SPACETRACK_USERNAME", raising=False)
    monkeypatch.delenv("SPACETRACK_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="SPACETRACK_USERNAME"):
        catalog.login(FakeSession())


def test_get_family_name_parenthetical_hint():
    assert catalog.get_family_name("COSMOS 2403 (GLONASS)") == "GLONASS"


def test_get_family_name_manual_override_word_boundary():
    assert catalog.get_family_name("EUTE 10A (EUTE W2A)") == "EUTELSAT"
    # Word-boundary matters -- a name that merely starts with the same
    # letters as an override key should not match it.
    assert catalog.get_family_name("NAVSTARX 1") != "NAVSTAR"


def test_get_family_name_suffix_stripping_fallback():
    assert catalog.get_family_name("TRANSIT 5B-1") == "TRANSIT"


def test_get_variant_name_rule():
    assert catalog.get_variant("GOES", "GOES 16", None) == "GOES-R SERIES"


def test_get_variant_date_band():
    assert catalog.get_variant("STARLINK", "STARLINK-1234", "2022-01-01") == "STARLINK V1.5"


def test_get_variant_falls_back_to_family_when_no_rule_matches():
    assert catalog.get_variant("HUBBLE", "HUBBLE", None) == "HUBBLE"


def test_fetch_and_build_rows_end_to_end(spacetrack_creds):
    data, rows = catalog.fetch_and_build_rows(FakeSession())

    assert data == SATCAT_RESPONSE
    assert rows == [
        {
            "NORAD_CAT_ID": "1",
            "OBJECT_NAME": "STARLINK-1234",
            "FAMILY_NAME": "STARLINK",
            "VARIANT_NAME": "STARLINK V1.5",
        },
        {
            "NORAD_CAT_ID": "2",
            "OBJECT_NAME": "COSMOS 2403 (GLONASS)",
            "FAMILY_NAME": "GLONASS",
            "VARIANT_NAME": "GLONASS-M",
        },
        {"NORAD_CAT_ID": "3", "OBJECT_NAME": "GOES 16", "FAMILY_NAME": "GOES", "VARIANT_NAME": "GOES-R SERIES"},
    ]


def test_refresh_catalog_writes_csv_and_json_and_backs_up_existing_file(tmp_path, spacetrack_creds):
    csv_path = tmp_path / "satellites.csv"
    json_path = tmp_path / "satellites.json"
    csv_path.write_text("NORAD_CAT_ID,OBJECT_NAME,FAMILY_NAME,VARIANT_NAME\n9,OLD,OLD,OLD\n")

    summary = catalog.refresh_catalog(FakeSession(), csv_path, json_path)

    assert summary["row_count"] == 3
    assert summary["family_count"] == 3
    assert csv_path.read_text().splitlines()[0] == "NORAD_CAT_ID,OBJECT_NAME,FAMILY_NAME,VARIANT_NAME"
    assert "STARLINK" in csv_path.read_text()
    assert json.loads(json_path.read_text()) == SATCAT_RESPONSE

    backup_path = summary["backup_path"]
    assert backup_path is not None
    assert "OLD" in open(backup_path).read()


def test_refresh_catalog_report_only_touches_no_files(tmp_path, spacetrack_creds):
    csv_path = tmp_path / "satellites.csv"
    json_path = tmp_path / "satellites.json"
    csv_path.write_text("NORAD_CAT_ID,OBJECT_NAME,FAMILY_NAME,VARIANT_NAME\n9,OLD,OLD,OLD\n")
    original = csv_path.read_text()

    summary = catalog.refresh_catalog(FakeSession(), csv_path, json_path, report_only=True)

    assert summary["row_count"] == 3
    assert csv_path.read_text() == original  # untouched
    assert not json_path.exists()
    assert summary["backup_path"] is None
    assert list(tmp_path.iterdir()) == [csv_path]  # no backup file created either


def test_refresh_catalog_raises_cleanly_on_login_failure(tmp_path, spacetrack_creds):
    csv_path = tmp_path / "satellites.csv"
    with pytest.raises(RuntimeError, match="login failed"):
        catalog.refresh_catalog(FakeSession(login_status=403), csv_path, tmp_path / "satellites.json")
    assert not csv_path.exists()


def test_refresh_catalog_raises_cleanly_on_missing_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("SPACETRACK_USERNAME", raising=False)
    monkeypatch.delenv("SPACETRACK_PASSWORD", raising=False)
    csv_path = tmp_path / "satellites.csv"
    with pytest.raises(RuntimeError, match="SPACETRACK_USERNAME"):
        catalog.refresh_catalog(FakeSession(), csv_path, tmp_path / "satellites.json")
    assert not csv_path.exists()
