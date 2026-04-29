"""Canonical Product schema.

Every record in the SQLite store and every row in the CSV/JSON exports conforms
to this model. The 12 fields requested by the spec map 1:1 onto the attributes
below; ``source_layer`` records *which* of the four extraction layers actually
filled each Product so downstream readers can audit provenance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


def _utcnow() -> datetime:
    """Timezone-aware UTC now (replaces deprecated datetime.utcnow)."""
    return datetime.now(timezone.utc)


class StockStatus(str, Enum):
    IN_STOCK = "in_stock"
    BACKORDER = "backorder"
    SPECIAL_ORDER = "special_order"
    OUT_OF_STOCK = "out_of_stock"
    UNKNOWN = "unknown"


# Map raw Algolia / JSON-LD strings onto our normalized enum.
_STOCK_NORMALIZATION = {
    "in stock": StockStatus.IN_STOCK,
    "instock": StockStatus.IN_STOCK,
    "https://schema.org/instock": StockStatus.IN_STOCK,
    "backorder": StockStatus.BACKORDER,
    "https://schema.org/backorder": StockStatus.BACKORDER,
    "special order": StockStatus.SPECIAL_ORDER,
    "out of stock": StockStatus.OUT_OF_STOCK,
    "outofstock": StockStatus.OUT_OF_STOCK,
    "https://schema.org/outofstock": StockStatus.OUT_OF_STOCK,
}


def normalize_stock(raw: str | None) -> StockStatus:
    if not raw:
        return StockStatus.UNKNOWN
    return _STOCK_NORMALIZATION.get(raw.strip().lower(), StockStatus.UNKNOWN)


class Price(BaseModel):
    amount: float
    currency: str = "USD"
    formatted: str | None = None


class AlternativeProduct(BaseModel):
    sku: str | None = None
    name: str | None = None
    url: str | None = None
    relation: str = "variation"  # variation | related | upsell


class Product(BaseModel):
    """Canonical product record.

    SKU is the natural primary key. Grouped products on Safco expose an SKU
    array (e.g. one per pack-size variation); we store all of them in
    ``additional_skus`` and pick the first as the canonical ``sku``.
    """

    model_config = ConfigDict(use_enum_values=True)

    # 1. product name
    name: str

    # 2. brand / manufacturer
    manufacturer: str | None = None

    # 3. SKU / item number / product code
    sku: str
    additional_skus: list[str] = Field(default_factory=list)

    # 4. category hierarchy (root -> leaf)
    categories: list[str] = Field(default_factory=list)

    # 5. product URL
    url: HttpUrl

    # 6. price (publicly visible)
    price: Price | None = None

    # 7. unit / pack size
    pack_size: str | None = None

    # 8. availability / stock indicator
    stock: StockStatus = StockStatus.UNKNOWN

    # 9. description
    description: str | None = None

    # 10. specifications / attributes
    specifications: dict[str, Any] = Field(default_factory=dict)

    # 11. image URL(s)
    images: list[str] = Field(default_factory=list)

    # 12. alternative products
    alternatives: list[AlternativeProduct] = Field(default_factory=list)

    # Provenance + run metadata
    source_layer: dict[str, str] = Field(
        default_factory=dict,
        description="Maps each filled field name to the layer that produced it: "
        "'algolia' | 'jsonld' | 'html' | 'llm'.",
    )
    scraped_at: datetime = Field(default_factory=_utcnow)

    def merge_from(self, other: "Product", layer: str) -> None:
        """Fill any None/empty fields on self with values from other.

        Used to combine the Algolia row (Layer 1) with product-page enrichment
        (Layers 2/3/4) without overwriting fields that already had values.
        Records the layer that contributed each new field for auditability.
        """
        for field in (
            "manufacturer",
            "pack_size",
            "description",
            "price",
        ):
            if getattr(self, field) in (None, "", {}, []):
                new_val = getattr(other, field)
                if new_val not in (None, "", {}, []):
                    setattr(self, field, new_val)
                    self.source_layer[field] = layer

        # Lists / dicts: union semantics
        for img in other.images:
            if img not in self.images:
                self.images.append(img)
                self.source_layer.setdefault("images", layer)

        for alt in other.alternatives:
            if not any(a.sku == alt.sku and alt.sku for a in self.alternatives):
                self.alternatives.append(alt)
                self.source_layer.setdefault("alternatives", layer)

        for k, v in other.specifications.items():
            if k not in self.specifications:
                self.specifications[k] = v
                self.source_layer.setdefault(f"specs.{k}", layer)

        if not self.categories and other.categories:
            self.categories = other.categories
            self.source_layer["categories"] = layer
