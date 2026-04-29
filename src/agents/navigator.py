"""Navigator agent.

Resolves free-text seed strings from config into concrete Algolia facet
filters by *facet discovery* on the products index itself.

Why facet discovery instead of querying the categories index? The Safco
categories index is incomplete — top-level taxonomy nodes like "Dental Exam
Gloves" don't exist as records there; they only appear as path segments
inside leaf records. Querying the products index for available facet values
on ``categories.level1`` (the taxonomy attribute we're going to filter on
anyway) gives us the authoritative list of crawl targets.

Matching strategy: prefer an exact case-insensitive match on the trailing
segment of the facet value. Falls back to substring containment so a seed
like "gloves" still finds "Dental Supplies /// Dental Exam Gloves".
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.algolia import AlgoliaClient
from src.core.logger import get_logger

log = get_logger(__name__)


@dataclass
class CategoryTarget:
    seed: str
    name: str               # the leaf segment, e.g. "Dental Exam Gloves"
    path: str               # full facet value, e.g. "Dental Supplies /// Dental Exam Gloves"
    facet_filter: list[list[str]]
    expected_count: int


# Algolia facet hierarchies use ' /// ' between levels in the products index.
_HIERARCHY_SEP = " /// "


def _split_facet_value(value: str) -> list[str]:
    return [p.strip() for p in value.split(_HIERARCHY_SEP) if p.strip()]


def _facet_attr_for_level(value: str) -> str:
    return f"categories.level{len(_split_facet_value(value)) - 1}"


class NavigatorAgent:
    def __init__(self, algolia: AlgoliaClient) -> None:
        self.algolia = algolia

    def _discover_level1_facets(self) -> dict[str, int]:
        """Map every facet value on ``categories.level1`` to its product count."""
        res = self.algolia.search(
            self.algolia.creds.products_index,
            params={
                "query": "",
                "hitsPerPage": 0,
                "page": 0,
                "facets": '["categories.level1"]',
            },
        )
        return res.get("facets", {}).get("categories.level1", {}) or {}

    def _match_seed(self, seed: str, facets: dict[str, int]) -> tuple[str, int] | None:
        seed_lower = seed.strip().lower()
        # Exact match on the leaf segment is the strongest signal.
        for value, count in facets.items():
            leaf = _split_facet_value(value)[-1].lower()
            if leaf == seed_lower:
                return value, count
        # Substring fallback (lets users pass partial names like "gloves").
        for value, count in facets.items():
            if seed_lower in value.lower():
                return value, count
        return None

    def resolve(self, seeds: list[str]) -> list[CategoryTarget]:
        facets = self._discover_level1_facets()
        log.info("navigator_facets_discovered", count=len(facets))

        targets: list[CategoryTarget] = []
        for seed in seeds:
            match = self._match_seed(seed, facets)
            if not match:
                log.warning("seed_unresolved", seed=seed)
                continue
            facet_value, count = match
            attr = _facet_attr_for_level(facet_value)
            target = CategoryTarget(
                seed=seed,
                name=_split_facet_value(facet_value)[-1],
                path=facet_value,
                facet_filter=[[f"{attr}:{facet_value}"]],
                expected_count=count,
            )
            log.info(
                "seed_resolved",
                seed=seed,
                name=target.name,
                facet=target.facet_filter,
                expected_count=count,
            )
            targets.append(target)
        return targets
