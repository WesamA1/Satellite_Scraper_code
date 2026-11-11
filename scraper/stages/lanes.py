"""Scrape lanes: each site gets its own worker going through the families, all
running at the same time, instead of one site after another for every family.

Why: a full catalog scrape is ~1,700 families, and one source at a time
meant every slow or throttled site held all the others up (a Wikimedia
throttle window lasts minutes). With lanes the run takes about as long as
the slowest site on its own.

Still polite: each lane makes one request at a time to its own site, with
the same pacing, backoffs and saved cooldowns as before (rate_limiter keeps
cooldowns per host, in the database). Sources that share a rate limit share
a lane: Wikimedia Commons, Wikidata and the Wikipedias all count against the
same Wikimedia bot account, so they go one after another in one lane.

A site that errors only affects its own lane: the lane waits, tries that
family once more, and moves on; after several failures in a row it gives up
and the other lanes carry on. Families a lane skipped aren't marked as
searched, so the next run tries them again.
"""

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

# Sources that share a rate limit with another source, by the lane they join.
SHARED_LANES = {"wikidata": "wikimedia", "wikipedia_multilang": "wikimedia"}

# After an error, wait this long before trying again (Stop still works).
ERROR_PAUSE_SECONDS = 120
# Each family gets this many tries in a lane before the lane moves on.
ATTEMPTS_PER_FAMILY = 2
# A lane gives up after this many failed tries in a row.
GIVE_UP_AFTER = 5


def group_sources(sources: tuple[str, ...] | list[str]) -> dict[str, tuple[str, ...]]:
    """Lane name -> its sources, in the order they were given."""
    lanes: dict[str, list[str]] = {}
    for source in sources:
        lanes.setdefault(SHARED_LANES.get(source, source), []).append(source)
    return {name: tuple(members) for name, members in lanes.items()}


@dataclass
class Lane:
    name: str
    sources: tuple[str, ...]
    total: int
    state: str = "waiting"  # waiting, running, pausing, done, stopped, gave_up
    families_done: int = 0
    family: str = ""
    source: str = ""
    item: str = ""
    images: int = 0
    failures_in_a_row: int = 0
    errors: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    source_seconds: dict[str, float] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "sources": list(self.sources),
            "state": self.state,
            "families_done": self.families_done,
            "total": self.total,
            "family": self.family,
            "source": self.source,
            "item": self.item,
            "images": self.images,
            "errors": len(self.errors),
            "last_error": self.errors[-1] if self.errors else None,
            "updated_at": self.updated_at,
        }


class LaneRun:
    """Runs `run_family(lane, family, conn, session)` for every family in
    every lane, one thread per lane (or in the calling thread when there's
    just one lane). Collects the counts the scrape job reports."""

    def __init__(
        self,
        lanes: dict[str, tuple[str, ...]],
        families: list[str],
        cancel: threading.Event,
        log: Callable[[str], None],
        on_change: Optional[Callable[[list[dict]], None]] = None,
        wait: Optional[Callable[[float], None]] = None,
    ):
        self.families = families
        self.cancel = cancel
        self.log = log
        self.on_change = on_change
        self.wait = wait or (lambda seconds: cancel.wait(seconds))
        self.lanes = {name: Lane(name, members, len(families)) for name, members in lanes.items()}
        self.lock = threading.Lock()
        self.images_found = 0
        self.on_family_done: Optional[Callable[[Lane], None]] = None

    # ---- shared counters, called from the lane threads ----

    def changed(self, lane: Lane, **fields) -> None:
        with self.lock:
            for key, value in fields.items():
                setattr(lane, key, value)
            lane.updated_at = time.time()
            snapshot = [ln.snapshot() for ln in self.lanes.values()]
        if self.on_change:
            self.on_change(snapshot)

    def add_images(self, lane: Lane, n: int) -> int:
        with self.lock:
            lane.images += n
            self.images_found += n
            return self.images_found

    def families_done(self) -> int:
        """Families every lane has finished (the slowest lane's count)."""
        with self.lock:
            return min((ln.families_done for ln in self.lanes.values()), default=0)

    # ---- running ----

    def run_lane(self, lane: Lane, conn: sqlite3.Connection, session, run_family) -> None:
        self.changed(lane, state="running")
        for family in self.families:
            if self.cancel.is_set():
                self.changed(lane, state="stopped")
                return
            if not self._family_with_retry(lane, family, conn, session, run_family):
                return  # gave up or stopped
            self.changed(lane, families_done=lane.families_done + 1, item="")
            if self.on_family_done:
                self.on_family_done(lane)
        self.changed(lane, state="done", family="", source="", item="")

    def _family_with_retry(self, lane: Lane, family: str, conn, session, run_family) -> bool:
        for attempt in range(1, ATTEMPTS_PER_FAMILY + 1):
            try:
                self.changed(lane, family=family, state="running")
                run_family(lane, family, conn, session)
                lane.failures_in_a_row = 0
                return True
            except Exception as e:
                message = f"{e.__class__.__name__}: {e}"
                lane.errors.append(f"{family}: {message}")
                lane.failures_in_a_row += 1
                if lane.failures_in_a_row >= GIVE_UP_AFTER:
                    self.log(
                        f"[{lane.name}] gave up after {GIVE_UP_AFTER} failures in a row (last: {message}). "
                        "The other sources carry on; the next run tries this one again."
                    )
                    self.changed(lane, state="gave_up")
                    return False
                retrying = attempt < ATTEMPTS_PER_FAMILY
                self.log(
                    f"[{lane.name}] {family}: {message}. Waiting {ERROR_PAUSE_SECONDS}s, then "
                    + ("trying it again." if retrying else "moving on (the next run tries it again).")
                )
                self.changed(lane, state="pausing")
                self.wait(ERROR_PAUSE_SECONDS)
                if self.cancel.is_set():
                    self.changed(lane, state="stopped")
                    return False
        lane.skipped.append(family)
        return True

    def run(self, open_lane: Callable[[Lane], tuple], close_lane: Callable[[Lane, tuple], None], run_family) -> None:
        """open_lane(lane) -> (conn, session) for that lane's thread."""
        lanes = list(self.lanes.values())
        if len(lanes) == 1:
            conn, session = open_lane(lanes[0])
            try:
                self.run_lane(lanes[0], conn, session, run_family)
            finally:
                close_lane(lanes[0], (conn, session))
            return

        failures: list[BaseException] = []

        def worker(lane: Lane) -> None:
            handles = None
            try:
                handles = open_lane(lane)
                self.run_lane(lane, *handles, run_family)
            except BaseException as e:  # a bug, not a site error: stop everything and report it
                failures.append(e)
                self.cancel.set()
                self.changed(lane, state="gave_up")
            finally:
                if handles is not None:
                    close_lane(lane, handles)

        threads = [
            threading.Thread(target=worker, args=(lane,), name=f"lane-{lane.name}", daemon=True) for lane in lanes
        ]
        for thread in threads:
            thread.start()
        try:
            for thread in threads:
                while thread.is_alive():
                    thread.join(0.5)
        except KeyboardInterrupt:
            self.cancel.set()  # each lane stops after its current family
            for thread in threads:
                thread.join()
            raise
        if failures:
            raise failures[0]

    # ---- results ----

    def all_finished(self) -> bool:
        return all(ln.families_done == len(self.families) for ln in self.lanes.values())

    def error_summary(self) -> Optional[str]:
        parts = []
        for lane in self.lanes.values():
            if lane.state == "gave_up":
                why = f" ({lane.errors[-1]})" if lane.errors else ""
                parts.append(f"{lane.name} gave up after {lane.families_done} families{why}")
            elif lane.skipped:
                parts.append(f"{lane.name} skipped {len(lane.skipped)} family(ies) after errors")
        return "; ".join(parts) or None

    def per_source_extras(self) -> dict[str, dict]:
        """Time spent and errors per source, for the run's summary."""
        out = {}
        for lane in self.lanes.values():
            for source in lane.sources:
                out[source] = {
                    "seconds": round(lane.source_seconds.get(source, 0.0), 1),
                    "lane": lane.name,
                    "lane_state": lane.state,
                    "errors": len(lane.errors),
                }
        return out
