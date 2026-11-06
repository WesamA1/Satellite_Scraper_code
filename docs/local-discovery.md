# Discovery on local models (lab computer)

Discovery finds candidate image sources, and code drafting writes a first
version of a source module for an approved candidate. By default both use
Claude through the Anthropic API. With `DISCOVERY_PROVIDER=local` they use a
model running on the lab computer instead, plus a search service you run
yourself (SearXNG). No API key and no per-use cost.

Nothing about the review steps changes: every result lands as a **pending**
candidate, a person approves or rejects it, drafted code is read before it's
promoted, and off-limits sites (`scraper/sources/off_limits.yaml`) are never
suggested.

## What runs where

| Piece | What it is | Default address |
|---|---|---|
| Model server | Ollama or vLLM, serving the OpenAI-style chat API | `http://localhost:11434/v1` (Ollama) or `http://localhost:8000/v1` (vLLM) |
| SearXNG | Self-hosted web search, in Docker | `http://localhost:8888` |
| Ground Station | This code, run with `uv` | `http://localhost:8000` for the web GUI; use `serve --port 8080` if vLLM has 8000 |

The lab computer needs internet access: SearXNG searches the web, and
Discovery reads each candidate site's pages.

## 1. Get the code onto the lab computer

```bash
git clone <repo-url> && cd Satellite_Scraper_code
git checkout feature/attribute-annotation
uv sync
cp .env.example .env   # then fill in what you need; .env is never committed
```

Discovery itself needs no scraping credentials. Leave the Space-Track and
Wikimedia keys blank if this machine won't scrape (only one machine scrapes at
a time anyway, since they share the Wikimedia bot account).

## 2. Run a model server

How a local search works: the model writes a few search queries, then judges
each site from up to three of its pages (about 6-8k tokens per site). Any
recent instruction-tuned model that can answer in JSON works. Suggestions by GPU
memory (check the Ollama library or Hugging Face for current names):

| GPU memory | Discovery model | Code drafting model |
|---|---|---|
| 24 GB | `qwen3:14b`, or `qwen3:30b` (a mixture-of-experts model, fast) | `qwen3-coder:30b` |
| 48 GB | `qwen3:32b` | `qwen3-coder:30b` |
| 80 GB+ | a 70B-class model, or `qwen3:32b` with room to spare | `qwen3-coder:30b` or larger |

### Option A: Ollama (simplest)

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3:32b
ollama pull qwen3-coder:30b
```

**Give it a big enough context window.** Ollama's default is small, and the
site pages would get cut off without telling you. Set it before the server
starts, for example in the systemd service (`sudo systemctl edit ollama`):

```ini
[Service]
Environment="OLLAMA_CONTEXT_LENGTH=16384"
```

then `sudo systemctl restart ollama`. The API is at `http://localhost:11434/v1`.

### Option B: vLLM (faster on a big GPU)

```bash
uv tool install vllm
vllm serve Qwen/Qwen3-32B --port 8000 --max-model-len 16384
```

The API is at `http://localhost:8000/v1`, and the model name in `.env` is the
one you served (`Qwen/Qwen3-32B`). Serve the coding model on another port, or
leave `LOCAL_CODEGEN_MODEL` blank to use the same model for both. If you start
vLLM with `--api-key`, put that key in `LOCAL_LLM_API_KEY`.

## 3. Run SearXNG

Make a folder with a `settings.yml` that turns on JSON answers (Discovery
needs them; they're off by default):

```bash
mkdir -p ~/searxng
cat > ~/searxng/settings.yml <<'YAML'
use_default_settings: true
server:
  secret_key: "replace-with-any-long-random-string"
  limiter: false
search:
  formats: [html, json]
YAML
docker run -d --name searxng --restart unless-stopped \
  -p 127.0.0.1:8888:8080 -v ~/searxng:/etc/searxng searxng/searxng
```

`127.0.0.1` keeps it reachable from the lab computer only. Check it:

```bash
curl -s "http://localhost:8888/search?q=landsat&format=json" | head -c 300
```

A `403` means the JSON format isn't on. Empty results usually mean the
search engines it uses are blocking it; enable a few more engines in
`settings.yml` (see the SearXNG docs).

## 4. Settings in `.env`

```
DISCOVERY_PROVIDER=local
LOCAL_LLM_URL=http://localhost:11434/v1
LOCAL_LLM_MODEL=qwen3:32b
LOCAL_CODEGEN_MODEL=qwen3-coder:30b
SEARXNG_URL=http://localhost:8888
# LOCAL_LLM_API_KEY=      only if the model server wants one
# LOCAL_LLM_TIMEOUT=600   seconds to wait for one answer
```

## 5. Check it works

```bash
uv run python -m scraper.cli doctor
```

Look for `discovery`, `discovery:local_model` and `discovery:search`, all
`PASS`. Then a test run that writes nothing:

```bash
uv run python -m scraper.local_discovery "LANDSAT satellite"
```

It prints the queries, which sites it left out and why, what it read, and the
candidates it would suggest.

## 6. Use it

- **One topic:** `uv run python -m scraper.cli discover --prompt "LANDSAT satellite images"`
- **Through the catalog:** `uv run python -m scraper.cli discover-auto --batch-size 5`
- **Only the gaps:** `uv run python -m scraper.cli discover-auto --only-missing --batch-size 5`
  searches only families with no images yet. Run it after a scrape.
- **In the web GUI:** `uv run python -m scraper.cli serve`, then Discovery. The
  line at the top says which model and search are in use; "Only families with
  no images yet" is the same gap search.

Then review as usual: approve or reject each candidate (Discovery page, or
`sources --pending` and `approve` / `reject`), draft code for an approved one
(`codegen <id>`), **read the draft**, and `promote <id>` only once you're
satisfied. A draft that isn't valid Python is refused instead of saved.

## How it behaves

- **Fixed steps, not free research.** The model writes queries, SearXNG runs
  them, the code reads each site, and the model judges each one in a fixed
  JSON shape. Smaller models handle that far more reliably than open-ended
  browsing.
- **Left out before reading:** off-limits sites, sites the scraper already
  has a source for (Wikimedia, NASA images, Europeana, DPLA, Smithsonian,
  Flickr), search engines, social media, shops, stock-photo sites, and sites
  already on the candidate list.
- **Turned away after reading:** sites whose terms appear to prohibit AI/ML
  training or text and data mining, and sites that don't host images of the
  topic. Both are logged, so you can see what was dropped.
- **Polite reading:** robots.txt is honored, requests to one site are spaced
  two seconds apart, pages are capped in size, and only web pages are read
  (never images).
- **A first reading, not a decision.** Each candidate's notes list the pages
  the model read. Check them before approving: a local model can misjudge
  licensing, and the review step is what the dataset's licensing depends on.

## Adding an off-limits site

Add it to `scraper/sources/off_limits.yaml` with the reason:

```yaml
- domain: example.org
  name: Example Archive
  reason: Terms prohibit use for machine learning.
```

Subdomains are covered too. This applies to Claude's searches as well.
