# Changelog

All notable changes to Ground Station. Newest first. This file is also
rendered in the app itself, under About -> Release Notes.

## Ground Station 3.0, November 1, 2026

The pipeline's top-down rewrite, carried the rest of the way: a new name, a
Scrape page that actually tells you what it's doing, licensing moved to
human judgment instead of a runtime gate, and several sources that were
quietly finding nothing now actually work.

### A new name
Renamed from "Scraper 3.0" to **Ground Station**, across the app, the CLI,
and the docs. "REV. 3.0" stays on the title screen as the version marker.

### Scrape, reworked
The Scrape page now shows which sources are actually ready to use
(pre-checked, tagged "ready") versus which still need an API key ("needs
key"), instead of defaulting to just Wikimedia and NASA regardless of what
else was promoted. While a scrape runs, it shows live which family and
source it's currently on, which item within that source, a running "images
found" count, and a warning if that status stops updating, so a slow source
no longer looks identical to a stuck one. "Retry failed searches" and
"Scrape from scratch" give you a way to re-open previously fruitless
searches without ever touching an image that's already downloaded.

### Licensing is now a human call
Whether a source's licensing is trustworthy is decided once, by a human,
during Discovery review: reading the generated source code before
promoting it. There's no automated per-item gate at scrape time anymore.
License fields are still recorded honestly for provenance, they just don't
block an image from being kept.

### Sources that actually work now
Europeana, DPLA, Smithsonian, and Flickr were almost never finding anything.
Their relevance matching required the literal satellite catalog name in
a title, which real-world titles almost never have. They now match on
domain keywords too, the same way Wikimedia always has.

### Reliability
Fixed a real hang that could freeze an entire scrape run indefinitely. A
CDN was half-closing a connection without the client's timeout ever firing;
downloads now run under a hard watchdog instead. Stop used to only check
between families, so a single slow family could take many minutes to
actually stop; it now checks between sources and between items too.
Wikidata and Wikipedia-multilang could crash the whole run on a malformed
response; both are hardened now. Finished job entries no longer pile up for
the life of the server process.

### Quality of life
The API Keys page links straight to the signup page for every source that
needs one. The Discovery page's less-used boxes collapse until needed.
Navigating to a page with a job already running on it (or clicking "View"
from the activity strip) now reattaches to that job's live log instead of
showing an idle form. The title screen picked up a small pixel-style
animation and an evener set of corner marks.
