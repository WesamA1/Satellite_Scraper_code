# Changelog

All notable changes to Ground Station. Newest first. This file is also
rendered in the app itself, under About -> Release Notes.

## Discovery on local models, November 6, 2026

### No API needed
Discovery and code drafting can now run on the lab computer's own models
instead of Claude: set `DISCOVERY_PROVIDER=local` and point it at a model
server (Ollama, vLLM, or anything with the OpenAI-style chat API) and a
self-hosted SearXNG search. `docs/local-discovery.md` walks through the
setup. Instead of free research, the local version runs fixed steps: the
model writes search queries, the code reads each site's main, terms and API
pages (honoring robots.txt, two seconds apart), and the model judges each
site in a fixed format. Search engines, stock sites, sites already used and
off-limits sites are left out; sites whose terms prohibit AI/ML training are
turned away. Results still land as pending candidates for review, with the
pages that were read listed in their notes.

### Also
- An off-limits list, `scraper/sources/off_limits.yaml` (Gunter's Space
  Page to start), that applies to Claude's searches too.
- Drafted code is checked to be valid Python before it's saved.
- Auto-discovery can search only families with no images yet ("Only
  families with no images yet", or `discover-auto --only-missing`).
- The Discovery page shows which model and search are in use, and the
  health check tests the model server and SearXNG.

## Scrape fixes, November 6, 2026

### A new image never overwrites a saved one
Downloaded files are named by their place in a site's search results, and
a site can return its results in a different order next time. A new image
could then get the same name as one already saved: it was downloaded over
the saved file, leaving that file's record and license describing a
different picture, and the scrape stopped on a duplicate-name error. Each
download now takes the next free name instead (..._03_2.jpg).

### USGS switched off
The USGS source was a placeholder that never contacted USGS (no API with a
per-item license for pictures of satellites was ever found), so it could
never find anything. It's no longer in the enabled list; the file stays in
case a real API turns up.

## Scrape lanes, November 6, 2026

### Every site at once
A scrape used to try the sources one after another for each family, so
one slow or throttled site held all the others up; the last full run was
on course for about 80 hours. Now each site runs in its own lane, side by
side, each still one request at a time with the same pacing and backoffs.
Wikimedia, Wikidata and the Wikipedias share a lane, since they share the
bot account. The Scrape page shows a row per lane: how far it has got,
what it's on, images found, and whether it's running, paused after an
error, done or gave up.

### One site's error no longer ends the run
A timeout or connection error used to stop the whole scrape. Now the lane
waits two minutes, tries that family once more, and moves on; after five
failures in a row only that lane gives up, and the Status page says so.
Each run's summary also records how long each source took, and scrapes
started from the web page are now written to the log file too.

## Pre-filled guesses, November 6, 2026

### Guesses for every image, ahead of time
The Train model page has a fourth step, **Pre-fill guesses**, that works out
the trained model's guesses for every image waiting for annotation in one
batch (or `prefill` from the command line). With **Auto-fill every image**
ticked on the Annotate page, each image then opens with its guesses already
filled in, instead of waiting for the image model one image at a time.
Nothing is submitted: annotators still check every field, and unsure guesses
(below 60%) stay marked. It can be stopped and picked up again; images that
already have the current model's guesses are skipped.

## Status at a glance, November 4, 2026

### An overview on the Status page
The top of the Status page now shows what needs attention (an off-limits
source switched on, new images that haven't been through the filter, a
source that never finds anything or is missing its API key, family
questions waiting in Review, annotations newer than the trained model),
where the images are (waiting for the filter, waiting for review, kept,
annotated, training examples, and what was set aside and why), coverage of
the satellite families, the recent scrapes, and how each source is doing.
Each scrape's summary is now saved, so runs show up there from now on.

### Fixes
The health check no longer reports Wikimedia as unreachable (it was being
turned away for not saying who it was), and counts a `hf auth login` as a
Hugging Face token. Downloads are now checked to really be images:
Europeana sometimes returns a PDF, even under a .jpg name, and those go to
quarantine instead of Review (four already in were set aside). Log files
are only created when something is logged, and test runs no longer write
into logs/.

## Review rules, November 4, 2026

### One recognizable satellite per image
Review now states the rule at the top: keep one recognizable satellite (a
photo or a rendering; partial views are fine), and reject images with
several satellites, a satellite you couldn't recognize (half-built, too
small or too blurry), or no satellite at all. Reject is now four one-click
reasons: Several satellites (S), Not recognizable (N), Not a satellite (X)
and Other (R). The reason is saved with the image, and so are the
automatic ones ("filter", "duplicate"), so the team's decisions can later
train a screening model. A check set page (linked from Train model) labels
a fixed sample of 200 images the same way, for scoring screening models.

### Housekeeping
The filter no longer scores or moves imported images. Gunter's Space Page,
which had been switched on, is off again, and the 57 images it downloaded
are quarantined (kept, not deleted).

## Tidier pages, November 4, 2026

### Less on the Annotate page
The Annotate page is now just setup, the session and the workspace.
Importing (a data folder, or just an annotations CSV) and exporting moved to
a new **Data** page in the sidebar, with the two imports merged into one box.
The Auto-fill options moved into the Session box, since they're set once per
session, and only show once a model has been trained. The hint repeated
under every field became one short legend at the top of each page.

### Embed is part of Train model
The Embed page is gone from the pipeline: embedding is step 2 of the Train
model page, which has the model picker and progress. Old links to it land
there. The `embed` command is unchanged.

## Auto-fill, November 4, 2026

### Let the model have a go
Once a model has been trained, the Annotate page's **Auto-fill** button
fills in the current image with the model's guesses, each tagged with how
sure it is ("model · 92%", amber when it's unsure). Fields it can't guess,
like Manufacturer, stay blank. It never submits: the annotator checks every
field, fixes what's wrong and submits as usual. **Show it filling in** walks
through the pages filling one field at a time, so you can watch it work
("Finish now" skips to the end). **Auto-fill every image** does it as each
image opens.

### See how it did
The check screen gets a Model column: ✓ where the answer matches the
model's guess, ✗ with what it said where it doesn't. Each submit reports how
many fields the model got right, with a running total for the session.
Guesses are saved with the model version, so how the model does in real use
can be measured over time. The first Auto-fill takes a few seconds while the
image model loads; after that it's near instant.

## Identical files, November 4, 2026

### Imported and scraped images, one pool
`import-data` (or "Import a data folder" on the Annotate page) adds a data
folder's images and answers to the database, so they go through the same
duplicate scan, Review and export as scraped images. When an imported image
is the same file as a scraped one, the imported copy is kept: it's the one
people already annotated. It takes over the scraped copy's source and
license record, and the scraped copy goes to quarantine. If the two are
filed under different families, Review asks which, starting on the
imported family. Files in the data folder are never moved, and re-running
only adds what's new. Rows the old CSV wrote into the wrong columns aren't
imported, so those images come up on the Annotate page to be labeled again.

### The data folder, and training anywhere
Annotated images and their CSVs now have a home: `data/` (`annotations.csv`
or `annotations/<annotator>.csv`, plus `images/`), gitignored so it's copied
between machines rather than committed. Building the training set, the new
`train` command and the Train model page all start from it. `train` runs all
three steps in one go, so training can move to a more powerful machine: copy
the project and `data/` over, run it, and copy the trained model file back.
Training sets store portable paths and each image's fingerprint. Families
that share an identical photo (the old data filed some under two names, like
ELEKTRO and ELEKTRO-L) are now kept on the same side of the train/test
split, so the model is never tested on a photo it trained on.

### Known variants
Review's variant list can now grow. Pick "Other variant…", type the name,
and leave "Add to this family's known variants" ticked: it's saved to
`known_variants.json` and offered for that family from then on. The
catalog's own variants still come from its split rules; catalog refreshes
never touch the known-variants file, which is kept in git so every machine
sees the same list. To remove a mistaken entry, edit that file.

### Identical copies are removed for you
The Review page's duplicate scan now tells byte-for-byte identical files
apart from look-alikes. Identical copies within the same family are removed
automatically: the best copy is kept (one with annotations first, then one
already reviewed, then the first downloaded) and the rest go to quarantine,
never deleted. Look-alikes, meaning the same or a similar picture in a
different file, still wait for a person, since two different photos can
look alike to the scan.

### Which family is this?
When the same file was downloaded under more than one family, Review asks
once which family it belongs to, offering the families it was found under
plus "Other family…" to search the whole satellite catalog. The answer
applies to every copy: one is kept, filed under that family and moved into
its folder, and the others go to quarantine. Keep waits until a family is
chosen; Reject rejects every copy. Quarantine moves also never overwrite a
file that's already there.

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
