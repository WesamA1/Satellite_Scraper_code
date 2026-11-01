"""Offline tests for Phase 1 Task 6: doctor. No real network calls -- the
reachability check takes an injected fetch_fn."""

from scraper.doctor import CheckResult, check_disk_space, check_gpu, run_checks


def test_gpu_check_warns_instead_of_crashing_without_torch():
    """This sandbox has no torch installed -- confirms the real, not hypothetical,
    ModuleNotFoundError path."""
    result = check_gpu()
    assert result.status in ("warn", "pass")  # warn here (no torch); pass on a real dev machine
    if result.status == "warn":
        assert "torch" in result.detail.lower()


def test_disk_space_check_reports_a_number():
    result = check_disk_space()
    assert result.name == "disk_space"
    assert "GB free" in result.detail


def test_reachability_check_never_raises_and_uses_injected_fetch():
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return url.startswith("https://commons")

    results = run_checks(fetch_fn=fake_fetch)
    reachability_results = [r for r in results if r.name.startswith("reachable:")]

    assert len(reachability_results) == 2
    assert len(calls) == 2  # no real requests.get ever happened
    by_name = {r.name: r.status for r in reachability_results}
    assert by_name["reachable:commons.wikimedia.org"] == "pass"
    assert by_name["reachable:images-api.nasa.gov"] == "fail"


def test_run_checks_includes_every_category():
    results = run_checks(fetch_fn=lambda url: True)
    names = {r.name for r in results}
    assert "gpu" in names
    assert "credential:SPACETRACK_USERNAME" in names
    assert "disk_space" in names
    assert "package:requests" in names
    assert all(isinstance(r, CheckResult) for r in results)
