"""AlgoliaExtractor — Layer 1, the primary extractor.

Maps a raw Algolia hit dict onto a canonical Product. This layer fills ~80% of
the spec fields by itself. Anything missing here is left None for downstream
layers (DetailExtractor, then the LLM fallback) to fill.
"""

from __future__ import annotations

import html
from typing import Any

from pydantic import ValidationError

from src.core.logger import get_logger
from src.core.schema import (
    AlternativeProduct,
    Price,
    Product,
    StockStatus,
    normalize_stock,
)

log = get_logger(__name__)


def _first_sku(raw: Any) -> tuple[str | None, list[str]]:
    """Algolia hits expose either a single SKU string or an array (grouped products)."""
    if isinstance(raw, list) and raw:
        return str(raw[0]), [str(s) for s in raw[1:]]
    if isinstance(raw, str) and raw:
        return raw, []
    return None, []


def _categories_from_hit(hit: dict[str, Any]) -> list[str]:
    """Pick the deepest available categories.levelN array, split on ' /// '."""
    cats = hit.get("categories") or {}
    if not isinstance(cats, dict):
        return []
    deepest_key = None
    for key in cats:
        if not key.startswith("level"):
            continue
        if deepest_key is None or int(key[5:]) > int(deepest_key[5:]):
            deepest_key = key
    if not deepest_key:
        return []
    values = cats[deepest_key] or []
    # Each entry is a full path; pick the one rooted at "Dental Supplies".
    for v in values:
        if isinstance(v, str) and v.startswith("Dental Supplies"):
            return [p.strip() for p in v.split("///")]
    return [p.strip() for p in str(values[0]).split("///")] if values else []


def _price_from_hit(hit: dict[str, Any]) -> Price | None:
    p = hit.get("price")
    if not isinstance(p, dict):
        return None
    usd = p.get("USD")
    if not isinstance(usd, dict):
        return None
    amount = usd.get("default")
    if amount in (None, "", 0):
        return None
    try:
        return Price(
            amount=float(amount),
            currency="USD",
            formatted=usd.get("default_formated"),
        )
    except (TypeError, ValueError):
        return None


def _images_from_hit(hit: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("image_url", "thumbnail_url"):
        v = hit.get(key)
        if isinstance(v, str) and v and v not in out:
            out.append(v)
    return out


class AlgoliaExtractor:
    def __init__(self) -> None:
        self.construction_failures = 0

    def hit_to_product(self, hit: dict[str, Any]) -> Product | None:
        sku, additional = _first_sku(hit.get("sku"))
        if not sku:
            log.warning("algolia_hit_missing_sku", object_id=hit.get("objectID"))
            return None

        name_raw = hit.get("name") or hit.get("family_title") or sku
        name = html.unescape(str(name_raw))

        url = hit.get("family_url") or hit.get("url")
        if not url:
            log.warning("algolia_hit_missing_url", sku=sku)
            return None

        price = _price_from_hit(hit)
        stock = normalize_stock(hit.get("stock_availability"))

        try:
            product = Product(
                name=name,
                manufacturer=hit.get("manufacturer_name") or None,
                sku=sku,
                additional_skus=additional,
                categories=_categories_from_hit(hit),
                url=url,
                price=price,
                stock=stock,
                images=_images_from_hit(hit),
                source_layer={
                    "name": "algolia",
                    "sku": "algolia",
                    "url": "algolia",
                    "manufacturer": "algolia" if hit.get("manufacturer_name") else "missing",
                    "categories": "algolia",
                    "price": "algolia" if price else "missing",
                    "stock": "algolia" if stock != StockStatus.UNKNOWN else "missing",
                    "images": "algolia" if hit.get("image_url") else "missing",
                },
            )
        except ValidationError as e:
            # Pydantic-level rejection (malformed URL, type mismatch, …) is
            # logged and the row is dropped rather than aborting the run.
            self.construction_failures += 1
            log.warning(
                "product_construction_failed",
                sku=sku,
                url=url,
                errors=[{"loc": err["loc"], "msg": err["msg"]} for err in e.errors()],
            )
            return None
        return product

    def alternatives_from_family(self, products: list[Product]) -> None:
        """Group products by their Algolia ``family_id`` and link them as variations.

        We piggy-back on the SKU as a proxy for product identity. Two products
        sharing the same canonical URL prefix or same first-token name are
        treated as a family. The simpler and more reliable signal — the
        Algolia ``family_id`` field — is recorded into ``source_layer`` upstream;
        if we ever expose it on Product we can switch to grouping by it
        directly. For the prototype, URL-based grouping covers the common
        case where one parent product fans out to several SKUs sharing a slug.
        """
        by_url: dict[str, list[Product]] = {}
        for p in products:
            by_url.setdefault(str(p.url), []).append(p)

        for url, group in by_url.items():
            if len(group) < 2:
                continue
            for this in group:
                for sib in group:
                    if sib is this or sib.sku == this.sku:
                        continue
                    if any(a.sku == sib.sku for a in this.alternatives):
                        continue
                    this.alternatives.append(
                        AlternativeProduct(
                            sku=sib.sku, name=sib.name, url=str(sib.url)
                        )
                    )
                    this.source_layer.setdefault("alternatives", "algolia")
