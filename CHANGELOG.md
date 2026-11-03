# Changelog

All notable changes to Ground Station. Newest first. This file is also
rendered in the app itself, under About -> Release Notes.

## Annotation and the first-pass model, November 3, 2026

The standalone desktop annotation tool now lives inside Ground Station, and
the first version of a model that learns from annotators' answers is here
to train. It will later pre-fill its best guess for annotators to confirm.

### Annotate and Attributes pages
The old PyQt annotation tool, rebuilt as two pages. **Annotate** works the
same way annotators already know: attributes one category at a time, five
per page, a check screen before submitting, and "Mark unusable". Missing
answers are now highlighted instead of the Next button doing nothing, going
back a page keeps what you entered, every annotation records who made it,
and the counter counts up ("Image 2 of 3,000"). **Attributes** is the old
Admin Panel: edit categories, attributes and options in the browser, with
the list checked before it's saved so a bad edit can't break the file.

### Unknown, and no silent defaults
Every attribute now has an Unknown button for "can't tell from this image",
saved in one spelling, so annotators never have to guess a manufacturer or
a mass to get past a required field. "None" still means "looked, and it
isn't there". Dropdowns start blank instead of on their first option, so a
field nobody touched can't quietly save an answer.

### Picking up from the old tool
Two ways to carry on where the old tool left off. Old-tool folder mode uses
its image folders and CSV directly (each submit adds a row and moves the
image, same as before), with "Fill in from old tool" reading its settings.
Or `import-annotations` brings the old CSV into the database. Rows are now
written by column name: the old tool wrote them by position, which put
values under the wrong headers once the attribute list was edited (334 of
1,907 rows in the existing CSV). Those rows are flagged, never imported.

### Attribute list changes
Color and Solar Panel Configuration are multi-selects now, matching how
annotators were already answering them ("Metallic and Gold", "Double Wing
and Surface"). Color gained Green, Orange, Red and Yellow. Antennas and
Deployment Mechanisms became multi-selects too.

### Train model page
A new page under Annotate, also available as commands. **Training set**
(`build-training-set`) turns old-tool CSVs (one per annotator) and Ground
Station's annotations into labeled examples, leaving out Unknown, unusable
and shifted answers, and splits them into train and test by satellite
family. **Embed** (`embed`) runs every image through a frozen DINOv3 or
SigLIP 2 model. **Train and score** (`train-heads`) trains one small
classifier per attribute and scores it on families it never saw, against a
baseline that always gives the most common answer, plus how many fields
would be pre-filled at 80% and 90% confidence and how often they'd be right.

### Setup
DINOv3 is gated on Hugging Face: log in with `hf auth login`, or save
`HF_TOKEN` on the API Keys page, on each machine. `torchvision` is a new
dependency; on the Windows GPU machine install it from the same CUDA index
as torch. Plain `pytest` now works without `python -m`.

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
