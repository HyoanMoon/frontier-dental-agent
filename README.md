# Frontier Dental Agent

A working prototype of an **agent-based scraping system** for the Safco Dental
Supply catalog. Built for the Frontier Dental AI take-home test.

The pipeline discovers products in two seeded categories
(`Dental Exam Gloves`, `Sutures & surgical products`), normalizes them into a
structured schema, persists the result in SQLite with idempotent UPSERTs, and
exports JSON + CSV for downstream consumption.

---

## TL;DR

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # paste ANTHROPIC_API_KEY (optional, see below)
python -m src.main run
```

That command crawls both target categories end-to-end, writes
`output/products.db` (SQLite) plus `output/products.json` and
`output/products.csv`, and prints a structured JSON log line per pipeline
event.

### Verify "AI as fallback, not decoration" in one query

Every Product row carries a `source_layer` column recording which extraction
layer filled each of its fields. After a finished run you can prove the
LLM-as-last-resort policy at a glance:

```bash
sqlite3 output/products.db "
WITH t AS (
  SELECT json_each.value AS layer
  FROM products, json_each(products.source_layer)
)
SELECT layer, COUNT(*) AS n,
       printf('%.2f%%', 100.0 * COUNT(*) / SUM(COUNT(*)) OVER ()) AS pct
FROM t GROUP BY layer ORDER BY n DESC;
"
```

Expected on the included sample dataset: ~80 % `algolia`, ~9 % `jsonld`,
~6 % `html`, ~5 % `llm`. Full breakdown and the spec-field mapping live
in [LLM usage policy](#where-each-field-actually-came-from-873-product-run).

---

## Architecture overview

The system is a **layered extraction pipeline** orchestrated through a small
team of single-responsibility agents. Each layer is a fallback for the next:
fast/cheap deterministic sources first, LLM only where it actually adds value.

```
                            ┌────────────────────┐
                            │   config.yaml      │
                            │   .env             │
                            └─────────┬──────────┘
                                      │
                                      ▼
 ┌──────────────┐   seeds   ┌───────────────────┐
 │  Navigator   │──────────►│  Algolia client   │  (creds discovered live
 │   agent      │  facets   │  /1/indexes/*/    │   from storefront HTML)
 └──────────────┘           │     queries       │
                            └─────────┬─────────┘
                                      │ hits
                                      ▼
 ┌──────────────────────────────────────────────────┐
 │                  Extractor chain                 │
 │                                                  │
 │  L1 AlgoliaExtractor   ~80% of fields            │
 │  L2 JSON-LD parser     description, breadcrumb   │
 │  L3 HTML/bs4 parser    spec table, gallery       │
 │  L4 LLM (Claude Haiku) pack size, irregular fmt  │
 └─────────────────────┬────────────────────────────┘
                       │ Product
                       ▼
 ┌──────────────┐   ┌──────────────┐    ┌──────────────┐
 │  Validator   │──►│   Store      │──► │  Exports     │
 │  + dedup     │   │  (SQLite)    │    │  JSON / CSV  │
 └──────────────┘   └──────┬───────┘    └──────────────┘
                           │
                    crawl_state checkpoint
                    (resumable)
```

### Why this approach

The Safco storefront is a Magento 2 site with a Hyvä (Alpine.js) theme. The
HTML you see in `view-source` contains client-side templates — the real
product list is filled in by JavaScript at render time.

Reverse-engineering the page revealed two stable, server-side data sources:

1. **Algolia search index.** The site embeds a session-scoped, time-limited
   public Algolia API key in its inline JavaScript and uses
   `https://<app>-dsn.algolia.net/1/indexes/*/queries` for every search,
   filter, and category listing. Hitting it directly returns the *exact*
   data the browser receives — fully structured, paginated, no DOM scraping
   required.
2. **JSON-LD on every product page.** Server-rendered `application/ld+json`
   blocks of `@type=Product` carry name / sku / price / availability /
   description / image / breadcrumb in plain text, independent of the
   client-side rendering.

Combining these two sources gives us **~90 % of the spec's required fields
deterministically and for free**. The remaining ~10 % (pack size, irregular
spec tables) is exactly the "extraction fallback for irregular layouts"
case the spec calls out as appropriate LLM use, so we route it there.

This is faster, cheaper, and far more reliable than HTML-only scraping with
LLM in the hot path.

---

## Agent responsibilities

| Module | Responsibility | Spec mapping |
|---|---|---|
| `agents/navigator.py` | Resolves free-text seed names (e.g. "Dental Exam Gloves") to concrete Algolia facet filters by discovering available `categories.level1` values via the products index. Demonstrates the spec's "navigation reasoning" capability. | navigator agent |
| `agents/classifier.py` | Heuristic-first page classifier (regex over JSON-LD type) with an LLM fallback for irregular pages. **Architectural hook only** — instantiated by the orchestrator but not invoked in the default Algolia-driven run, since the Algolia path returns canonical product hits and bypasses page-type discovery. Wired so a generic-crawler fallback (e.g. when the JSON-LD subcategory recovery in `recovery.py` is wired through to product extraction) can plug it in without restructuring. | page classifier |
| `agents/extractor_algolia.py` | Layer 1 — primary extractor. Maps an Algolia hit to a canonical `Product`; covers ~80 % of fields. | extractor agent |
| `agents/extractor_detail.py` | Layers 2/3/4 — fallback chain. Parses JSON-LD on the product page, falls back to HTML/bs4 selectors, falls back to LLM for the residual gaps (pack size, spec table). | extractor agent + LLM fallback |
| `agents/validator.py` | Pydantic schema validation, SKU-keyed deduplication with merge-on-conflict, missing-field quality report. | validator / deduplicator |
| `agents/recovery.py` | Cross-cutting recovery: JSON-LD subcategory crawl when Algolia is unavailable, and an LLM-backed "selector repair suggestion" stub for when CSS selectors drift. | retry / recovery logic |
| `core/http.py` | Token-bucket rate limiter (per-host), Tenacity-driven retries with exponential backoff, robots.txt enforcement, custom User-Agent. | rate limiting + retries + error handling |
| `core/store.py` | SQLite with `INSERT … ON CONFLICT DO UPDATE` (idempotent), JSON + CSV exports, `crawl_state` checkpoint table for resumable runs. | persistence + idempotency + checkpointing |
| `core/llm.py` | Anthropic SDK wrapper. Hard `max_calls_per_run` budget cap. Auto-disabled when no API key is present. | LLM (selectively) |
| `core/algolia.py` | Live credential discovery + multi-query / facet-filter / paginated search. | external API integration |
| `core/config.py` | YAML config loader with full pydantic validation. | config-driven execution |
| `core/logger.py` | structlog JSON logger; every line carries the `run_id` so a full pipeline run is replayable through `jq`. | logging |
| `orchestrator.py` | Wires the agents together; tracks `(run_id, category, page)` checkpoints. | overall pipeline |
| `main.py` | CLI entry: `run`, `export`, `info` subcommands. | runnable project |

---

## Setup & execution instructions

### 1. Install

```bash
git clone <this repo>
cd frontier-dental-agent
python -m venv venv
source venv/bin/activate              # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env — paste your Anthropic API key:
#   ANTHROPIC_API_KEY=sk-ant-api03-...
```

The system runs without a key — the LLM fallback layer is auto-disabled and
`pack_size` / `specifications` rates drop accordingly. All other layers
(Algolia, JSON-LD, HTML) work identically.

Get a key at <https://console.anthropic.com/>. \$5 of credit is more than
enough for a dozen full pipeline runs.

### 3. Run

```bash
# Full pipeline against both target categories
python -m src.main run

# Smoke test — small N per category, no enrichment, no LLM
python -m src.main run --max-per-category 5 --no-enrich

# Resume an interrupted run by passing its run id
python -m src.main run --run-id <id-from-prior-log>

# Re-export from existing DB without re-fetching
python -m src.main export --format all

# Show row count
python -m src.main info
```

### 3a. Run via Docker (alternative to local Python)

```bash
docker build -t frontier-scraper .
docker run --rm \
  --env-file .env \
  -v "$(pwd)/output:/app/output" \
  frontier-scraper
```

The image is built in a multi-stage Dockerfile, runs as a non-root user, and
treats `/app/output` as a volume so the SQLite database and JSON/CSV exports
land on the host. Override the default command to run other subcommands:

```bash
docker run --rm -v "$(pwd)/output:/app/output" frontier-scraper info
docker run --rm -v "$(pwd)/output:/app/output" frontier-scraper export --format csv
```

### 4. Inspect output

```
output/
├── products.db        # SQLite, queryable directly
├── products.json      # full normalized dataset
├── products.csv       # flat tabular export
└── run.log.jsonl      # structured event log
```

```bash
# Query SQLite directly
sqlite3 output/products.db 'SELECT name, manufacturer, price_amount FROM products LIMIT 5;'

# Replay logs for a specific run
jq 'select(.run_id == "abc123def456")' output/run.log.jsonl
```

---

## Output schema

Every row written to SQLite / JSON / CSV conforms to `src/core/schema.py`'s
`Product` model:

| Column | Type | Spec field | Notes |
|---|---|---|---|
| `sku` | str (PK) | SKU / item number | Canonical SKU. Grouped products with multiple SKUs use the first as canonical and store the rest in `additional_skus`. |
| `name` | str | product name | HTML entities are unescaped. |
| `url` | str | product URL | Always the canonical clean URL (`/product/<slug>`). |
| `manufacturer` | str \| null | brand / manufacturer | Algolia's `manufacturer_name`, which is the *real* manufacturer (Halyard, Ethicon, etc.) — not the seller. |
| `categories` | JSON array | category hierarchy | Root → leaf, e.g. `["Dental Supplies", "Dental Exam Gloves", "Nitrile gloves"]`. |
| `price_amount` | float \| null | price | Numeric. |
| `price_currency` | str \| null | price | ISO code. |
| `price_formatted` | str \| null | price | Display string from Algolia (e.g. "\$31.49"). |
| `pack_size` | str \| null | unit / pack size | e.g. "200/box", "100ct". Sourced from product name (regex), description (regex), or LLM. |
| `stock` | enum | availability | `in_stock` \| `backorder` \| `special_order` \| `out_of_stock` \| `unknown`. |
| `description` | str \| null | description | From the product-page JSON-LD `description`. |
| `specifications` | JSON object | specifications / attributes | Key/value pairs parsed from the spec table; falls back to LLM for irregular pages. |
| `images` | JSON array | image URL(s) | Deduplicated union of Algolia thumbnails, JSON-LD images, and gallery elements from the product page. |
| `alternatives` | JSON array | alternative products | Each entry: `{sku, name, url, relation}`. Linked via shared canonical URL across SKUs (variations). |
| `additional_skus` | JSON array | — | All non-canonical SKUs for grouped/variation products. |
| `source_layer` | JSON object | — (provenance) | Maps each filled field to the layer that produced it: `algolia` \| `jsonld` \| `html` \| `llm`. Use this to audit data quality. |
| `scraped_at` | ISO datetime | — | When this Product was constructed in-memory. |
| `last_seen_at` | ISO datetime | — | Updated on every UPSERT — useful for staleness checks across runs. |

### Field coverage by extraction layer

| Spec field | Layer producing it | Confidence | Coverage on a 873-product run |
|---|---|---|---|
| product name | L1 Algolia | ✅ deterministic | **100 %** |
| SKU | L1 Algolia | ✅ deterministic | **100 %** |
| product URL | L1 Algolia | ✅ deterministic | **100 %** |
| category hierarchy | L1 Algolia | ✅ deterministic | **100 %** |
| price | L1 Algolia, L2 JSON-LD fallback | ✅ deterministic | **100 %** |
| availability | L1 Algolia, L2 JSON-LD fallback | ✅ deterministic | **100 %** |
| image URL(s) | L1 + L2 union | ✅ deterministic | **100 %** |
| description | L2 JSON-LD | ✅ deterministic | **100 %** |
| brand / manufacturer | L1 Algolia | ✅ deterministic | **99.4 %** |
| alternative products | L1 (URL grouping) | ✅ deterministic | **98.1 %** |
| unit / pack size | L3 regex / L4 LLM | ⚠️ LLM-assisted | 81 % (capped by `max_calls_per_run=500`) |
| specifications / attributes | L3 HTML / L4 LLM | ⚠️ LLM-assisted | 42 % (same cap) |

The bottom two rows are limited by the default LLM call cap (500/run). Raise
`llm.max_calls_per_run` in `config.yaml` to increase coverage at the cost of
more API spend (~\$0.04 per additional 500 calls on Claude Haiku 4.5).

---

## LLM usage policy

The LLM is **strictly a fallback**. It never sees a field that prior layers
produced. Three concrete invocation paths in the prototype:

1. **`extract_pack_size`** — when the regex chain in
   `agents/extractor_detail.py` fails to find a unit/pack size in the product
   name or description, send the name + description to Claude Haiku and ask
   for a structured JSON answer.
2. **`extract_specifications`** — when no spec table can be parsed via bs4
   selectors, send a cleaned text excerpt of the product page and ask for a
   key/value JSON object.
3. **`classify_page`** (stubbed for irregular pages) — heuristic regex is
   tried first; LLM only invoked when the page does not match either of the
   `application/ld+json` markers.

A fourth path — **`suggest_selector`** in `agents/recovery.py` — is wired
but not invoked in the default run; it's there to demonstrate the
"selector repair suggestions" capability the spec mentions, and would fire
in production when historical pages start failing a known selector.

A hard **`max_calls_per_run`** cap (default: 500) in `config.yaml` acts as
a cost circuit-breaker; the LLM client refuses to make further calls past
the cap and logs `llm_budget_exhausted`. Default model is **Claude Haiku
4.5**. The 873-product run included with this repo cost ≈ \$0.05 against
the 500-call cap; raise the cap in `config.yaml` to trade more spend for
higher pack-size / specs coverage. The actual `llm_calls` count for any
run is reported in the `run_completed` log line and the `RunSummary` dataclass.

### Where each field actually came from (873-product run)

Every Product row carries a `source_layer` JSON column whose values are one of
`algolia` / `jsonld` / `html` / `llm` / `missing`. Aggregating those values
across all 9,792 field-fillings in the persisted run gives a precise
distribution of where the data came from:

| Layer | Source | Field-fillings | Share |
|---|---|---|---|
| 1 | Algolia (deterministic API) | 7,816 | **79.82 %** |
| 2 | JSON-LD on product page (deterministic) | 892 | **9.11 %** |
| 3 | HTML / regex on product page (deterministic) | 589 | **6.02 %** |
| 4 | LLM (Claude Haiku, fallback only) | 490 | **5.00 %** |
| — | left empty (no layer produced a value) | 5 | 0.05 % |

This is the audit trail behind the "use AI/agents only where they add
practical value" claim: 95 % of the data is deterministic, the LLM owns
only the fields where regular extraction genuinely couldn't reach (pack
sizes embedded in product names, irregular spec tables). Reproduce the
table yourself with one SQL query:

```sql
sqlite3 output/products.db "
WITH t AS (
  SELECT json_each.value AS layer
  FROM products, json_each(products.source_layer)
)
SELECT layer, COUNT(*) AS n,
       printf('%.2f%%', 100.0 * COUNT(*) / SUM(COUNT(*)) OVER ()) AS pct
FROM t GROUP BY layer ORDER BY n DESC;
"
```

---

## Production-minded design (per spec checklist)

| Concern | Where it's handled |
|---|---|
| **Rate limiting** | `core/http.py` — per-host token buckets (`algolia: 5/s, burst 10` ; `safco: 2/s, burst 5`) configurable in `config.yaml`. |
| **Retries** | `core/http.py` — Tenacity, exponential backoff (1 → 30 s), max 5 attempts. Retries on connection errors, timeouts, 5xx, 429 (respects `Retry-After`). 4xx fail fast. |
| **Error handling** | Per-product try/except in the extractor chain — one failed product never aborts a run. Errors logged with category, URL, and exception type. Algolia outage triggers the `RecoveryAgent` JSON-LD fallback. |
| **Resumability / checkpointing** | `crawl_state` table in SQLite with `(run_id, category, page, status)` rows. `--run-id <id>` reuses an existing run id; `Store.done_categories()` lists categories that have **completed every step (list → validate → enrich → persist)**, and the orchestrator skips those entirely. Categories killed mid-enrichment stay `in_progress` / `failed` and are re-fetched on resume — UPSERT on `products` merges the rows that *were* persisted before the kill with the freshly-fetched data. Per-product checkpoints *within* a category are not tracked, so a re-fetch reprocesses every product in that category — see *Limitations*. |
| **Logging** | structlog JSON, every line carries `run_id`. File sink configured via `logging.file` in `config.yaml`; `jq`-friendly. |
| **Deduplication** | Pydantic SKU-keyed dedup in `ValidatorAgent.validate` with merge-on-conflict; SQLite `PRIMARY KEY(sku)` + `INSERT … ON CONFLICT DO UPDATE` provides a second line of defense at the storage layer. |
| **Idempotency** | Storage UPSERT means N successive runs converge to the same DB state. The data-only fields (`name`, `price`, etc.) reflect the latest fetch; `last_seen_at` records when. |
| **Config-driven** | Everything user-tunable lives in `config.yaml` — seeds, rate limits, retry counts, LLM toggle/cap, storage paths, logging level. Validated via pydantic at load time so typos fail fast. |
| **Secrets** | `python-dotenv` loads `.env` once at CLI entry; `.env` is in `.gitignore`. Algolia credentials are NOT secrets — they are session-scoped, time-limited public search keys re-discovered on every run. |
| **Deployment path** | See [Scaling to full-site crawling](#scaling-to-full-site-crawling) below. |

---

## Limitations

These are real, not hypothetical. Each is paired with the production
mitigation that would lift it.

### Coverage
- **Algolia API key is session-scoped (~24 h TTL).** The key is re-discovered
  from the storefront HTML on every run. If Safco changes the embedding
  pattern, our regex breaks. Mitigation: `RecoveryAgent` falls back to a
  JSON-LD-driven subcategory crawl.
- **Two categories only**, as scoped. Adding a category is a one-line
  config change. Full-site coverage (32 800 products in the index) requires
  iterating all `categories.level1` facets — the navigator already
  enumerates them; we just don't filter the seeds list down today.
- **Pack-size extraction** is regex-first with LLM fallback. Accuracy on the
  pack-size patterns we observed is high but truly novel formats may
  return null.

### Data quality
- **Specifications coverage is partial.** Many Safco product pages don't
  have an explicit attribute table; the LLM fallback fills in some of these
  but leaves the field empty when the page genuinely has no spec data.
- **Alternative products** are derived from products that share a canonical
  URL (the variation pattern). The on-page "you might also like" widget
  loads via a different undocumented recommendations API which we have
  not reverse-engineered for this prototype.
- **Multi-currency:** USD only — the storefront only exposes USD.

### Operational
- **Single-process, single-worker.** Acceptable for ~870 products at our
  rate-limit budget (~3 minutes); a production crawl across all 32k
  products would parallelize across categories on a job runner.
- **No incremental mode.** Each run re-fetches everything. Production
  extension: compare `algoliaLastUpdateAtCET` to a high-water mark in
  `crawl_state` and only re-fetch hits with a newer timestamp.
- **LLM budget cap is hard-coded per run.** A real cost circuit-breaker
  would query account spend in addition to call count.

### Resumability granularity
- **Category-level only.** `--run-id <id>` skips categories whose entire
  list → validate → enrich → persist cycle finished in a previous run
  (status `done` in `crawl_state`). A category that was partially processed
  when the previous run died is re-fetched and reprocessed from scratch —
  UPSERT keeps the database consistent (the rows persisted before the kill
  are safely merged with the new fetch) but the network and LLM cost for
  the *whole* category is paid again. A production extension would
  checkpoint at the per-product level (e.g. one row per processed SKU
  inside `crawl_state`) to make mid-category resumes truly free.
- **`Store.pending_state()` is reserved for that future per-product
  checkpoint.** It returns rows whose status is `pending` /
  `in_progress` / `failed`, but no caller in the prototype reads it; the
  orchestrator only consults `done_categories()`. Kept as a public API so
  a per-product resume can plug in without a schema change.

### Recovery / fallback paths (be honest about stubs)
- **Algolia → JSON-LD recovery is a URL-collection stub, not a hardened
  fallback.** When `algolia.list_products` raises, the orchestrator calls
  `RecoveryAgent.jsonld_subcategory_urls` which parses the category page's
  JSON-LD ItemList and returns product URLs. The URLs are *logged but
  never converted into Product records* in this prototype — wiring them
  through `DetailExtractor` is straightforward (one loop) but was out of
  scope for the take-home. Status: architectural hook present, real
  recovery left as a future extension.
- **Selector-repair suggestions are an architectural hook, not an
  auto-trigger.** `RecoveryAgent.suggest_selector` is a working
  LLM-backed function but the orchestrator never invokes it. Real use
  needs multi-run drift detection (which selectors started failing N
  times in a row vs. their historical success rate), which requires
  a metrics store this prototype doesn't ship.
- **HTTP 200 with empty `hits` array is not treated as a failure.**
  Algolia returning zero results is currently indistinguishable from
  "this category is empty." Production would alert when expected count
  drops sharply run-over-run.
- **`failed_validation` counter is post-construction quality check, not
  pydantic validation.** `Product(**…)` raising `ValidationError` is
  caught separately in `AlgoliaExtractor.hit_to_product` and reported
  via `construction_failures`. The two counters are intentionally
  distinct.

### Testing
- **End-to-end run hits the live site.** The architecture is decoupled
  enough to mock (`HttpClient`, `AlgoliaClient`, `LLMClient` are all
  injected through the orchestrator), but no fixture-recorded test suite
  ships with the prototype. Adding `pytest` + recorded HTTP cassettes is
  a straightforward extension.

---

## Failure handling

The pipeline is designed to **degrade rather than crash** wherever practical.
The table below states what's actually wired in the prototype vs. what's a
stub or future extension — being explicit here matters more than overclaiming.

| Failure mode | What happens | Status |
|---|---|---|
| 5xx / 429 from Algolia or Safco | Tenacity retries with exponential backoff up to 5 attempts; 4xx fails fast. | ✅ wired |
| Network blip / timeout on a single product page | The product-page `GET` is wrapped in try/except inside `DetailExtractor.enrich`; product is returned with whatever fields had already been filled. Logged as `detail_fetch_failed`. | ✅ wired |
| Exception inside JSON-LD / HTML / LLM enrichment layer | Each enrichment layer is wrapped in its own try/except in `DetailExtractor.enrich`; a failure in one layer does not block the others. Logged as `detail_layer_failed`. | ✅ wired |
| Pydantic `ValidationError` while constructing a Product from an Algolia hit | Caught in `AlgoliaExtractor.hit_to_product`; the row is dropped, `construction_failures` counter incremented, error fields logged via `product_construction_failed`. | ✅ wired |
| One product blowing up during enrichment / persist | `Orchestrator.run` wraps the per-product loop body in try/except; logs `product_processing_failed` and increments `per_product_failures`. | ✅ wired |
| robots.txt disallows a URL | `RobotsBlocked` raised at fetch time; the URL is skipped and logged. | ✅ wired |
| LLM call fails / returns malformed JSON | `LLMClient._ask_json` catches and returns None; field stays null. **Note:** the LLM is the last layer in the chain, so a None here means the field is left unfilled; nothing else attempts to recover it. | ✅ wired |
| LLM budget exhausted (`max_calls_per_run`) | `LLMClient._can_call` returns False; subsequent fallback calls become no-ops. | ✅ wired |
| Process killed mid-run | On restart, pass `--run-id <id>`. The `done` marker is only written **after every product in a category has been UPSERT'd**, so a category killed mid-enrichment stays `in_progress` / `failed` and is re-fetched on resume. The half-persisted rows from the previous attempt are safely UPSERT-merged with the new fetch (no duplicates, no loss). Categories that completed before the kill are skipped. Per-product checkpoints inside a category are not tracked, so re-fetch reprocesses everything in that one category — see *Limitations*. | ✅ wired (category-level, partial-kill safe) |
| Pydantic-passed but spec-required field empty (`sku` / `name` / `url`) | `ValidatorAgent.validate` increments `failed_validation` and drops the row. (This is a quality gate, distinct from the `ValidationError` catch above.) | ✅ wired |
| **Algolia call raises** (network, auth, schema change) | Orchestrator catches the exception, logs `algolia_failed`, calls `RecoveryAgent.jsonld_subcategory_urls` which collects candidate product URLs from the category page's JSON-LD ItemList and logs the count. **Those URLs are not currently re-routed back into the extraction pipeline** — wiring them through `DetailExtractor` to produce real Product records is documented in *Limitations* as a future extension. | ⚠️ **prototype stub** — URL collection only; no product recovery |
| **Selector drift** (CSS selector matches nothing across many pages) | `RecoveryAgent.suggest_selector` exists as an LLM-backed hook for the spec's "selector repair suggestions" use case. Not invoked in the default run because real drift detection requires multi-run history. | ⚠️ **architectural hook** — wired, not auto-triggered |
| **Algolia returns HTTP 200 with 0 hits** | Currently treated as "this category is empty"; no fallback fires. If 0 hits is unexpected (i.e. taxonomy changed), this would silently under-count. A production fix would alert when expected count drops sharply between runs. | ❌ **not handled** — documented limitation |

---

## Scaling to full-site crawling

The prototype touches ~874 products in two categories. Scaling to the full
catalog (~32 800 products across 40+ level-1 categories) needs three
changes — none of them rewrites:

1. **Seed expansion.** Replace the two seeds in `config.yaml` with the full
   `categories.level1` enumeration the navigator already discovers. Or run
   the navigator without seeds and have it crawl every facet.
2. **Parallelism.** Run one orchestrator per seed in parallel; SQLite is
   fine up to a few writers (WAL mode is already enabled), or swap
   `core/store.py` to Postgres for higher concurrency. The `crawl_state`
   table already keys on `(run_id, category, page)` so workers don't
   collide.
3. **Incremental crawling.** Track each product's `algoliaLastUpdateAtCET`
   in the store and skip hits whose timestamp hasn't advanced since the
   last successful run. This makes hourly or daily refresh runs nearly
   free.

Deployment path:

```
Stage 1 — Local CLI            (current)
Stage 2 — Containerized        (Dockerfile + 'docker run')
Stage 3 — Scheduled Cloud Run  (cron-driven full refresh)
Stage 4 — Workflow runner      (Airflow / Prefect / Dagster
                                with one DAG node per category +
                                a final dedup/export node)
```

---

## Monitoring data quality

The prototype already emits the building blocks of a quality dashboard:

- **`source_layer` per Product** — a per-row JSON object recording which
  layer filled each field. Lets you compute "coverage rate" per layer
  over time.
- **`ValidatorAgent.report`** — counts of products missing each desired
  field, written into the run-completion log line:
  ```json
  { "event": "validation_complete", "missing": {"description": 4, ...} }
  ```
- **`llm_calls` in `RunSummary`** — tracks how often we needed the LLM
  fallback. A spike is a leading indicator that selectors have drifted.

In production we would:

1. Pipe `run.log.jsonl` to a log warehouse (Datadog / Loki / BigQuery).
2. Define alerts on rising `failed_validation` counts, rising `missing.*`
   rates, or sudden `llm_calls` spikes (selector drift).
3. Run a nightly **golden-page diff** — re-extract a fixed set of known
   products and compare to a reference snapshot. Drift on a previously-
   correct field is a strong signal of upstream change.
4. Surface `last_seen_at` staleness — products not seen in N runs are
   either delisted upstream or evidence of a coverage gap.

---

## Repository layout

```
frontier-dental-agent/
├── README.md
├── requirements.txt
├── config/
│   └── config.yaml             # all runtime knobs
├── .env.example
├── src/
│   ├── main.py                 # CLI
│   ├── orchestrator.py         # pipeline coordinator
│   ├── agents/
│   │   ├── navigator.py
│   │   ├── classifier.py
│   │   ├── extractor_algolia.py
│   │   ├── extractor_detail.py
│   │   ├── validator.py
│   │   └── recovery.py
│   └── core/
│       ├── schema.py           # Product pydantic model
│       ├── config.py           # YAML loader + validation
│       ├── http.py             # rate limit + retry + robots
│       ├── algolia.py          # creds discovery + search
│       ├── llm.py              # Anthropic wrapper + budget cap
│       ├── store.py            # SQLite + exports + checkpoint
│       └── logger.py           # structlog JSON
├── tests/                      # (smoke tests; see Limitations)
├── output/                     # generated; .gitignored except samples
└── samples/                    # curated representative products
```
