"""DetailExtractor — Layers 2, 3, and 4.

Given a product URL, fetches the page once and runs a fallback chain:

    Layer 2: parse JSON-LD ``@type=Product`` for description, image, breadcrumb
    Layer 3: bs4 selectors + regex on HTML body for spec table, pack size,
             additional images
    Layer 4: LLM extraction for fields the prior layers failed to fill

Returns a Product instance with only the *enrichment* fields set; the caller
is expected to merge it onto the Algolia-built Product via ``Product.merge_from``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup

from src.core.http import HttpClient
from src.core.llm import LLMClient
from src.core.logger import get_logger
from src.core.schema import Price, Product, normalize_stock

log = get_logger(__name__)


_PACK_SIZE_REGEXES = [
    re.compile(r"\b(\d+\s*/\s*(?:box|case|pkg|bag|pack|carton|tray|bx|cs))\b", re.I),
    re.compile(r"\b(\d+\s*ct)\b", re.I),
    re.compile(r"\b(\d+\s*(?:per|/)\s*(?:box|case|pkg|bag|pack))\b", re.I),
    re.compile(r"\b(\d+\s*(?:boxes?|cases?|packs?)\s*/\s*(?:case|carton))\b", re.I),
]


def _extract_pack_size_from_text(text: str) -> str | None:
    for r in _PACK_SIZE_REGEXES:
        m = r.search(text or "")
        if m:
            return m.group(1).strip()
    return None


def _extract_jsonld_blocks(html: str) -> list[Any]:
    out = []
    for m in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.S,
    ):
        try:
            out.append(json.loads(m.group(1).strip()))
        except json.JSONDecodeError:
            continue
    return out


def _flatten_jsonld(blocks: list[Any]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for block in blocks:
        items = block if isinstance(block, list) else [block]
        for it in items:
            if isinstance(it, dict):
                flat.append(it)
    return flat


class DetailExtractor:
    def __init__(self, http: HttpClient, llm: LLMClient) -> None:
        self.http = http
        self.llm = llm

    def enrich(self, base: Product) -> Product:
        """Fetch the product page and merge enrichment fields onto ``base``.

        Each enrichment layer is independently isolated: a parse error in
        JSON-LD does not prevent HTML/LLM layers from running; an LLM
        exception leaves the product untouched rather than killing the run.
        """
        try:
            r = self.http.get(str(base.url))
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001 — non-fatal for a single product
            log.warning("detail_fetch_failed", url=str(base.url), error=str(e))
            return base

        html_text = r.text

        for layer_name, fn in (
            ("jsonld", self._apply_jsonld),
            ("html", self._apply_html),
            ("llm", self._apply_llm),
        ):
            try:
                fn(base, html_text)
            except Exception as e:  # noqa: BLE001 — degrade per-layer
                log.warning(
                    "detail_layer_failed",
                    layer=layer_name,
                    sku=base.sku,
                    error=str(e),
                )

        return base

    # ── Layer 2 ─────────────────────────────────────────────────────────

    def _apply_jsonld(self, base: Product, html_text: str) -> None:
        items = _flatten_jsonld(_extract_jsonld_blocks(html_text))
        product_node = next(
            (it for it in items if it.get("@type") == "Product"), None
        )
        if not product_node:
            return

        if not base.description and product_node.get("description"):
            base.description = str(product_node["description"]).strip()
            base.source_layer["description"] = "jsonld"

        # Images: JSON-LD often has higher-quality URLs than the Algolia thumbnail
        imgs = product_node.get("image")
        if isinstance(imgs, str):
            imgs = [imgs]
        if isinstance(imgs, list):
            for u in imgs:
                if isinstance(u, str) and u and u not in base.images:
                    base.images.append(u)
                    base.source_layer.setdefault("images", "jsonld")

        # Price fallback — Algolia is usually right but JSON-LD is canonical
        offers = product_node.get("offers") or {}
        if isinstance(offers, dict) and not base.price:
            try:
                amount = float(offers.get("price"))
                base.price = Price(
                    amount=amount,
                    currency=str(offers.get("priceCurrency") or "USD"),
                )
                base.source_layer["price"] = "jsonld"
            except (TypeError, ValueError):
                pass

        # Availability fallback
        if base.stock in ("unknown", None) or (
            hasattr(base.stock, "value") and base.stock.value == "unknown"
        ):
            avail = (offers or {}).get("availability") if isinstance(offers, dict) else None
            if avail:
                base.stock = normalize_stock(str(avail)).value
                base.source_layer["stock"] = "jsonld"

        # Categories fallback via BreadcrumbList
        if not base.categories:
            crumb = next(
                (it for it in items if it.get("@type") == "BreadcrumbList"), None
            )
            if crumb and isinstance(crumb.get("itemListElement"), list):
                names = []
                for el in crumb["itemListElement"]:
                    item = el.get("item") if isinstance(el, dict) else None
                    if isinstance(item, dict) and item.get("name"):
                        names.append(str(item["name"]))
                if names:
                    base.categories = names
                    base.source_layer["categories"] = "jsonld"

    # ── Layer 3 ─────────────────────────────────────────────────────────

    def _apply_html(self, base: Product, html_text: str) -> None:
        # Pack size from name first, then description
        if not base.pack_size:
            ps = _extract_pack_size_from_text(base.name) or _extract_pack_size_from_text(
                base.description or ""
            )
            if ps:
                base.pack_size = ps
                base.source_layer["pack_size"] = "html"

        # Specifications: scan a handful of common Magento markup variants.
        if not base.specifications:
            specs = _parse_spec_table(html_text)
            if specs:
                base.specifications = specs
                base.source_layer["specifications"] = "html"

        # Additional product images from the gallery
        soup = BeautifulSoup(html_text, "html.parser")
        for img in soup.select(".product.media img, .gallery-placeholder img, .fotorama__img"):
            src = img.get("src") or img.get("data-src")
            if isinstance(src, str) and src.startswith("http") and src not in base.images:
                base.images.append(src)

    # ── Layer 4 ─────────────────────────────────────────────────────────

    def _apply_llm(self, base: Product, html_text: str) -> None:
        if not self.llm.enabled:
            return

        if not base.pack_size:
            ps = self.llm.extract_pack_size(base.name, base.description)
            if ps:
                base.pack_size = ps
                base.source_layer["pack_size"] = "llm"
                log.info("llm_filled_pack_size", sku=base.sku, value=ps)

        if not base.specifications:
            # Trim HTML to the meaningful body — strip head, scripts, style.
            soup = BeautifulSoup(html_text, "html.parser")
            for tag in soup(["script", "style", "noscript", "head"]):
                tag.decompose()
            cleaned = " ".join(soup.get_text(" ").split())
            specs = self.llm.extract_specifications(cleaned)
            if specs:
                base.specifications = specs
                base.source_layer["specifications"] = "llm"
                log.info("llm_filled_specs", sku=base.sku, count=len(specs))


# ── helpers ───────────────────────────────────────────────────────────

def _parse_spec_table(html_text: str) -> dict[str, str]:
    """Try several Magento spec-table variants. Empty dict if none matched."""
    soup = BeautifulSoup(html_text, "html.parser")
    out: dict[str, str] = {}

    # Variant 1: Magento default attribute table
    for tr in soup.select(
        "#product-attribute-specs-table tr, .additional-attributes-wrapper tr"
    ):
        th = tr.select_one("th")
        td = tr.select_one("td")
        if th and td:
            key = th.get_text(strip=True)
            val = td.get_text(" ", strip=True)
            if key and val:
                out[key] = val

    # Variant 2: definition list
    if not out:
        for dl in soup.select(".product-attributes, dl.product-info-attributes"):
            keys = [dt.get_text(strip=True) for dt in dl.find_all("dt")]
            vals = [dd.get_text(" ", strip=True) for dd in dl.find_all("dd")]
            for k, v in zip(keys, vals):
                if k and v:
                    out[k] = v

    return out
