"""Offline tests for scraper/sources/base.py's stream_to_file() -- the
watchdog-protected download write added after a real production hang: a CDN
half-closed a connection (socket sat in CLOSE-WAIT) without requests' own
read timeout ever firing, so a `for chunk in resp.iter_content(...)` loop
blocked forever and froze the entire scrape job. No real network calls here
-- a fake response object simulates both the normal and the stuck case.
"""

import threading

from scraper.sources.base import stream_to_file


class FakeResponse:
    def __init__(self, chunks=None, stall_event=None):
        self._chunks = chunks or []
        self._stall_event = stall_event

    def iter_content(self, chunk_size=8192):
        if self._stall_event is not None:
            self._stall_event.wait()  # blocks until the test releases it (or forever, if it doesn't)
        yield from self._chunks


def test_writes_all_chunks_on_a_normal_response(tmp_path):
    dest = tmp_path / "image.jpg"
    resp = FakeResponse(chunks=[b"hello ", b"world"])

    assert stream_to_file(resp, dest, timeout_seconds=5) is True
    assert dest.read_bytes() == b"hello world"


def test_gives_up_and_leaves_no_partial_file_when_the_stream_stalls(tmp_path):
    dest = tmp_path / "image.jpg"
    never_releases = threading.Event()  # deliberately never .set()
    resp = FakeResponse(chunks=[b"never arrives"], stall_event=never_releases)

    result = stream_to_file(resp, dest, timeout_seconds=0.2)

    assert result is False
    assert not dest.exists()  # no corrupt/truncated file left behind for a retry to trip over


def test_removes_a_partial_file_if_iter_content_raises_mid_write(tmp_path):
    dest = tmp_path / "image.jpg"

    class RaisingResponse:
        def iter_content(self, chunk_size=8192):
            yield b"partial bytes"
            raise ConnectionError("connection reset")

    assert stream_to_file(RaisingResponse(), dest, timeout_seconds=5) is False
    assert not dest.exists()


def test_rejects_an_html_page_served_back_instead_of_the_real_image(tmp_path):
    """Confirmed live: a source's download URL pointed at an HTML viewer
    page for most of one family's hits from one provider, saved to disk and
    recorded as a real image, extension and all (one literally ".jsp",
    derived from the URL rather than the actual content)."""
    dest = tmp_path / "image.jpg"
    resp = FakeResponse(chunks=[b"<!DOCTYPE html>\n<html><head>", b"<title>Consent</title></head></html>"])

    assert stream_to_file(resp, dest, timeout_seconds=5) is False
    assert not dest.exists()


def test_accepts_real_image_bytes_that_happen_to_start_with_whitespace(tmp_path):
    """The markup check only has to avoid false-rejecting genuine media --
    confirming it doesn't trip on leading whitespace before real bytes."""
    dest = tmp_path / "image.jpg"
    resp = FakeResponse(chunks=[b"\n\n\xff\xd8\xff\xe0", b"rest of a real jpeg"])

    assert stream_to_file(resp, dest, timeout_seconds=5) is True
    assert dest.read_bytes() == b"\n\n\xff\xd8\xff\xe0rest of a real jpeg"
