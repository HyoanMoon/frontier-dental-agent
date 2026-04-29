"""Validator + deduplicator.

Two responsibilities, both deterministic (no LLM):
1. Schema validation — already enforced by pydantic at construction time;
   here we add a *quality* check that flags products missing fields the spec
   explicitly requested, so the orchestrator can log gaps.
2. Deduplication — Algolia returns one row per parent + one per variation in
   some cases. We dedupe by SKU (the natural primary key) and merge data
   from duplicates rather than dropping them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.core.logger import get_logger
from src.core.schema import Product

log = get_logger(__name__)


# Spec-required fields that we can realistically expect on most rows.
_REQUIRED_FIELDS = ("name", "sku", "url")
_DESIRED_FIELDS = ("manufacturer", "categories", "price", "description", "images")


@dataclass
class QualityReport:
    total: int = 0
    missing: dict[str, int] = field(default_factory=dict)
    failed_validation: int = 0


class ValidatorAgent:
    def __init__(self) -> None:
        self.report = QualityReport()

    def validate(self, products: list[Product]) -> list[Product]:
        ok: list[Product] = []
        seen: dict[str, Product] = {}
        for p in products:
            if not p.sku or not p.name or not p.url:
                self.report.failed_validation += 1
                log.warning("product_failed_required_check", sku=p.sku, name=p.name)
                continue

            if p.sku in seen:
                # Merge: prefer non-empty values from the newer record
                seen[p.sku].merge_from(p, layer="dedup_merge")
                log.info("product_deduped", sku=p.sku)
                continue

            seen[p.sku] = p
            ok.append(p)

            # Track desired-field gaps for the data-quality report
            for field in _DESIRED_FIELDS:
                v = getattr(p, field)
                if v in (None, "", [], {}):
                    self.report.missing[field] = self.report.missing.get(field, 0) + 1

        # ``total`` accumulates across calls so the report stays accurate
        # when the orchestrator invokes validate() once per category rather
        # than once for the whole run.
        self.report.total += len(ok)
        log.info(
            "validation_complete",
            total=self.report.total,
            failed=self.report.failed_validation,
            missing=self.report.missing,
        )
        return ok
