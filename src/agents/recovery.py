"""Recovery agent.

Encapsulates the cross-cutting "what do we do when something failed?"
behaviour. The HTTP layer already handles retry/backoff at the transport
level; this agent operates above that, dealing with semantic failures.

PROTOTYPE STATUS — what is and isn't wired:

- ``jsonld_subcategory_urls`` IS wired but only collects candidate URLs.
  The orchestrator triggers it when ``algolia.list_products`` raises an
  exception; it returns a list of product URLs parsed from the category
  page's server-rendered JSON-LD ItemList. The URLs are logged but NOT
  fed back into the extraction pipeline — converting them into Product
  records would require running each URL through DetailExtractor and is
  documented as a future extension in README.md.

- ``suggest_selector`` IS wired but never invoked in the default run; it
  exists as a working hook for the spec's "selector repair suggestions"
  LLM use case. Production use would need multi-run drift detection to
  decide *when* to call it.

Both paths are exposed so the architecture can be extended without
restructuring; neither is a hardened recovery primitive in the prototype.
"""

from __future__ import annotations

import json
import re

from src.core.http import HttpClient
from src.core.llm import LLMClient
from src.core.logger import get_logger

log = get_logger(__name__)


class RecoveryAgent:
    def __init__(self, http: HttpClient, llm: LLMClient) -> None:
        self.http = http
        self.llm = llm

    # ── fallback 1: Algolia → JSON-LD subcategory crawl ────────────────

    def jsonld_subcategory_urls(self, category_url: str) -> list[str]:
        """Return product URLs found in the category page's JSON-LD ItemList."""
        try:
            r = self.http.get(category_url)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            log.warning("recovery_fetch_failed", url=category_url, error=str(e))
            return []

        out: list[str] = []
        for m in re.finditer(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            r.text,
            re.S,
        ):
            try:
                data = json.loads(m.group(1).strip())
            except json.JSONDecodeError:
                continue
            items = data if isinstance(data, list) else [data]
            for it in items:
                if not isinstance(it, dict):
                    continue
                if it.get("@type") == "ItemList" and "itemlist" in (
                    it.get("@id") or ""
                ):
                    for el in it.get("itemListElement", []):
                        url = el.get("url") or (el.get("item") or {}).get("url")
                        if isinstance(url, str) and "/product/" in url:
                            out.append(url)
        log.info("recovery_jsonld_urls_found", url=category_url, count=len(out))
        return out

    # ── fallback 2: selector repair (stubbed — see module docstring) ──

    def suggest_selector(self, html_excerpt: str, target_field: str) -> str | None:
        """Ask the LLM for a replacement CSS selector for ``target_field``.

        Returns None if the LLM is disabled or could not produce a selector.
        Real production use would also validate the suggestion against several
        golden pages before persisting it back to config.
        """
        if not self.llm.enabled:
            return None
        out = self.llm._ask_json(  # type: ignore[attr-defined] — internal helper
            system=(
                "You suggest a robust CSS selector to extract a field from "
                "Magento product HTML. Reply ONLY with JSON: "
                '{"selector": "<css selector>"} or {"selector": null}.'
            ),
            user=f"Field: {target_field}\nHTML:\n{html_excerpt[:6000]}",
        )
        if not out:
            return None
        sel = out.get("selector")
        return sel if isinstance(sel, str) and sel.strip() else None
