"""Algolia client.

Two responsibilities:
1. Discover live Algolia credentials by parsing the storefront HTML once at
   startup. The site embeds a session-scoped, time-limited public search key
   in its inline JavaScript; hard-coding it would mean the scraper breaks
   within ~24 hours of when the key was last rotated.
2. Issue paginated, faceted multi-queries against the products and categories
   indexes via the standard ``/1/indexes/*/queries`` endpoint.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from typing import Any

from src.core.http import HttpClient
from src.core.logger import get_logger

log = get_logger(__name__)


@dataclass
class AlgoliaCreds:
    application_id: str
    api_key: str
    products_index: str
    categories_index: str

    @property
    def endpoint(self) -> str:
        return f"https://{self.application_id.lower()}-dsn.algolia.net/1/indexes/*/queries"


# The storefront embeds creds inline. We pull a single chunk that mentions the
# Algolia config and then extract the three fields. If the embedding pattern
# changes the recovery layer can repair this regex.
_APP_ID_RE = re.compile(r'"applicationId"\s*:\s*"([A-Z0-9]+)"')
_API_KEY_RE = re.compile(r'"apiKey"\s*:\s*"([A-Za-z0-9+/=]+)"')
_INDEX_RE = re.compile(r'"indexName"\s*:\s*"([a-z0-9_]+)"')


def discover_creds(http: HttpClient, base_url: str) -> AlgoliaCreds:
    """Fetch any storefront page and parse out live Algolia credentials."""
    url = f"{base_url.rstrip('/')}/catalog/gloves"
    log.info("algolia_creds_discover_start", url=url)
    r = http.get(url)
    r.raise_for_status()
    html = r.text

    app_id_m = _APP_ID_RE.search(html)
    api_key_m = _API_KEY_RE.search(html)
    index_m = _INDEX_RE.search(html)
    if not (app_id_m and api_key_m and index_m):
        raise RuntimeError("Failed to discover Algolia credentials in storefront HTML")

    base_index = index_m.group(1)  # e.g. "safco_prod_default"
    creds = AlgoliaCreds(
        application_id=app_id_m.group(1),
        api_key=api_key_m.group(1),
        products_index=f"{base_index}_products",
        categories_index=f"{base_index}_categories",
    )
    log.info(
        "algolia_creds_discovered",
        application_id=creds.application_id,
        products_index=creds.products_index,
    )
    return creds


class AlgoliaClient:
    def __init__(self, http: HttpClient, creds: AlgoliaCreds) -> None:
        self.http = http
        self.creds = creds

    def _headers(self) -> dict[str, str]:
        return {
            "X-Algolia-Application-Id": self.creds.application_id,
            "X-Algolia-API-Key": self.creds.api_key,
            "Content-Type": "application/json",
        }

    def search(
        self,
        index: str,
        params: dict[str, Any] | None = None,
        facet_filters: list[list[str]] | None = None,
    ) -> dict[str, Any]:
        params = dict(params or {})
        if facet_filters is not None:
            params["facetFilters"] = json.dumps(facet_filters)
        param_str = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        body = {"requests": [{"indexName": index, "params": param_str}]}

        r = self.http.post(self.creds.endpoint, headers=self._headers(), json=body)
        r.raise_for_status()
        return r.json()["results"][0]

    # ── higher-level helpers ────────────────────────────────────────────

    def find_category(self, name: str) -> dict[str, Any] | None:
        """Fuzzy-match a free-text seed to a categories-index record."""
        result = self.search(
            self.creds.categories_index,
            params={"query": name, "hitsPerPage": 5, "page": 0},
        )
        hits = result.get("hits", [])
        if not hits:
            return None

        # Prefer exact case-insensitive match on category name; otherwise
        # take the highest-popularity hit Algolia ranks first.
        for h in hits:
            if h.get("name", "").lower() == name.lower():
                return h
        return hits[0]

    def list_products(
        self,
        facet_filter: list[list[str]],
        hits_per_page: int = 100,
        max_pages: int = 20,
    ) -> list[dict[str, Any]]:
        """Stream all products matching a facet filter.

        Algolia returns pages until ``page >= nbPages``. We respect
        ``max_pages`` as a runaway guard.
        """
        out: list[dict[str, Any]] = []
        page = 0
        nb_pages = 1
        while page < min(nb_pages, max_pages):
            res = self.search(
                self.creds.products_index,
                params={"query": "", "hitsPerPage": hits_per_page, "page": page},
                facet_filters=facet_filter,
            )
            hits = res.get("hits", [])
            out.extend(hits)
            nb_pages = res.get("nbPages", 0)
            log.info(
                "algolia_page_fetched",
                page=page,
                nb_pages=nb_pages,
                hits=len(hits),
                total_so_far=len(out),
            )
            page += 1
        return out
