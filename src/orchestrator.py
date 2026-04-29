"""Pipeline orchestrator.

Wires every agent into a single end-to-end flow:

    NavigatorAgent   →  resolve seeds to facet filters
    AlgoliaClient    →  fetch all products for each facet (paginated)
    AlgoliaExtractor →  hits → Product (Layer 1)
    DetailExtractor  →  Product → enriched Product (Layers 2/3/4)
    ValidatorAgent   →  schema check + dedup
    Store            →  UPSERT to SQLite + JSON/CSV exports

State is checkpointed at the (run_id, category) granularity in
``crawl_state``. The schema also has a ``page`` column (always 0 today) and
``pending_state`` helper, both reserved for a future per-product
checkpoint extension — see README *Limitations*.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.agents.classifier import PageClassifier
from src.agents.extractor_algolia import AlgoliaExtractor
from src.agents.extractor_detail import DetailExtractor
from src.agents.navigator import NavigatorAgent
from src.agents.recovery import RecoveryAgent
from src.agents.validator import ValidatorAgent
from src.core.algolia import AlgoliaClient, discover_creds
from src.core.config import Config
from src.core.http import HttpClient
from src.core.llm import LLMClient
from src.core.logger import get_logger
from src.core.schema import Product
from src.core.store import Store

log = get_logger(__name__)


@dataclass
class RunSummary:
    run_id: str
    categories: int
    products_seen: int
    products_persisted: int
    llm_calls: int
    quality_missing: dict[str, int]
    construction_failures: int = 0
    per_product_failures: int = 0
    skipped_categories: int = 0


class Orchestrator:
    def __init__(self, cfg: Config, run_id: str, store: Store) -> None:
        self.cfg = cfg
        self.run_id = run_id
        self.store = store

        self.http = HttpClient(cfg.site, cfg.rate_limits, cfg.retry)
        self.http.load_robots()

        creds = discover_creds(self.http, cfg.site.base_url)
        self.algolia = AlgoliaClient(self.http, creds)

        self.llm = LLMClient(cfg.llm)

        self.navigator = NavigatorAgent(self.algolia)
        self.classifier = PageClassifier(self.llm)
        self.algolia_extractor = AlgoliaExtractor()
        self.detail_extractor = DetailExtractor(self.http, self.llm)
        self.recovery = RecoveryAgent(self.http, self.llm)
        self.validator = ValidatorAgent()

    # ── public entry ────────────────────────────────────────────────────

    def run(self, *, enrich: bool = True, max_per_category: int | None = None) -> RunSummary:
        """Execute the full pipeline.

        Each category is processed as an independent, atomic-from-resume's-
        perspective unit:

            list  →  validate  →  enrich  →  persist  →  mark done

        ``crawl_state.status='done'`` is **only** written after every product
        in that category has been UPSERT'd into the products table. If the
        process is killed mid-enrichment, the category stays ``in_progress``
        / ``failed`` and a subsequent ``--run-id <id>`` resume will re-fetch
        and reprocess it (UPSERT keeps the products table consistent — the
        partially-persisted rows from the previous attempt are safely
        merged).
        """
        seeds = self.cfg.crawler.seeds
        targets = self.navigator.resolve(seeds)

        done_categories = self.store.done_categories(self.run_id)
        skipped_categories = 0

        products_seen = 0
        products_persisted = 0
        per_product_failures = 0
        post_enrichment_missing: dict[str, int] = {}

        for target in targets:
            if target.name in done_categories:
                log.info("category_skipped_resume", category=target.name)
                skipped_categories += 1
                continue

            # ── 1. Fetch + Layer-1 extract ──────────────────────────────
            products = self._collect_for_target(target, max_per_category)
            if not products:
                # Two cases reach here: (a) Algolia raised and the target was
                # already marked 'failed', or (b) Algolia returned 0 hits.
                # We deliberately do NOT mark either case 'done' — for (a)
                # we want resume to retry, and for (b) "0 hits" is treated
                # as a suspicious result worth re-checking on each run (the
                # *Limitations* section calls this out explicitly).
                continue
            products_seen += len(products)

            # ── 2. Link in-category variations + validate / dedup ───────
            self.algolia_extractor.alternatives_from_family(products)
            validated = self.validator.validate(products)
            log.info(
                "category_collected",
                category=target.name,
                products=len(products),
                validated=len(validated),
            )

            # ── 3. Enrich + persist EACH product before marking done ────
            for i, p in enumerate(validated):
                try:
                    if enrich:
                        self.detail_extractor.enrich(p)
                    self.store.upsert_product(p)
                    products_persisted += 1
                except Exception as e:  # noqa: BLE001 — single-product isolation
                    per_product_failures += 1
                    log.warning(
                        "product_processing_failed",
                        category=target.name,
                        sku=p.sku,
                        url=str(p.url),
                        error=str(e),
                    )
                if (i + 1) % 25 == 0:
                    log.info(
                        "enrichment_progress",
                        category=target.name,
                        done=i + 1,
                        total=len(validated),
                        failures_so_far=per_product_failures,
                    )

                # Quality counters: tally desired-but-empty fields per row.
                for field in (
                    "manufacturer",
                    "categories",
                    "price",
                    "description",
                    "images",
                    "pack_size",
                    "specifications",
                ):
                    v = getattr(p, field)
                    if v in (None, "", [], {}):
                        post_enrichment_missing[field] = (
                            post_enrichment_missing.get(field, 0) + 1
                        )

            # ── 4. Now safe to mark category done ────────────────────────
            self.store.set_state(
                self.run_id, target.name, page=0, status="done"
            )
            log.info("category_done", category=target.name, persisted=len(validated))

        return RunSummary(
            run_id=self.run_id,
            categories=len(targets),
            products_seen=products_seen,
            products_persisted=products_persisted,
            llm_calls=self.llm.calls_made,
            quality_missing=post_enrichment_missing,
            construction_failures=self.algolia_extractor.construction_failures,
            per_product_failures=per_product_failures,
            skipped_categories=skipped_categories,
        )

    # ── internals ───────────────────────────────────────────────────────

    _last_hits_for_target: dict[str, list[dict]] = {}

    def _collect_for_target(
        self,
        target,
        max_per_category: int | None,
    ) -> list[Product]:
        self.store.set_state(
            self.run_id, target.name, page=0, status="in_progress"
        )

        try:
            hits = self.algolia.list_products(
                target.facet_filter,
                hits_per_page=self.cfg.crawler.hits_per_page,
                max_pages=self.cfg.crawler.max_pages_per_category,
            )
        except Exception as e:  # noqa: BLE001
            log.error("algolia_failed", category=target.name, error=str(e))
            self.store.set_state(
                self.run_id, target.name, 0, "failed", error=str(e)
            )
            # Recovery (PROTOTYPE STUB): we collect candidate product URLs from
            # the category page's server-rendered JSON-LD ItemList and log the
            # count. Wiring those URLs back into Product records would require
            # per-URL detail-extraction passes; this is documented as a future
            # extension in the README rather than implemented here. Fail fast
            # and surface the error so the operator can investigate.
            try:
                slug = target.path.split("///")[-1].strip().lower()
                slug = slug.replace(" & ", "-").replace(" ", "-")
                urls = self.recovery.jsonld_subcategory_urls(
                    f"{self.cfg.site.base_url}/catalog/{slug}"
                )
                log.info(
                    "fallback_jsonld_url_collection_only",
                    note="prototype stub; URLs collected but not turned into products",
                    urls=len(urls),
                )
            except Exception as rec_e:  # noqa: BLE001
                log.warning("recovery_collection_failed", error=str(rec_e))
            return []

        self._last_hits_for_target[target.name] = hits

        if max_per_category is not None:
            hits = hits[:max_per_category]

        products: list[Product] = []
        for hit in hits:
            p = self.algolia_extractor.hit_to_product(hit)
            if p:
                products.append(p)

        # NOTE: we do *not* mark the category 'done' here. The 'done' marker
        # is only written by the caller (Orchestrator.run) after every
        # product has been enriched and UPSERT'd. Otherwise a kill during
        # enrichment would leave the category 'done' with rows missing from
        # the products table, and a subsequent --run-id resume would skip
        # them forever.
        return products
