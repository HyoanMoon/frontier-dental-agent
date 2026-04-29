"""PageClassifier — heuristic-first, LLM-fallback page typing.

99%+ of pages we touch are clearly classifiable from a quick markup check;
``classify`` returns the type without calling the LLM. The LLM is only invoked
when the heuristics return ``IRREGULAR`` and the caller has explicitly enabled
LLM fallback.
"""

from __future__ import annotations

import re
from enum import Enum

from src.core.llm import LLMClient
from src.core.logger import get_logger

log = get_logger(__name__)


class PageType(str, Enum):
    PRODUCT_DETAIL = "product_detail"
    CATEGORY_LISTING = "category_listing"
    IRREGULAR = "irregular"


_PRODUCT_LD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>[^<]*"@type"\s*:\s*"Product"',
    re.S,
)
_ITEMLIST_LD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>[^<]*"@type"\s*:\s*"ItemList"',
    re.S,
)


class PageClassifier:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def classify(self, html: str, *, use_llm: bool = False) -> PageType:
        if _PRODUCT_LD_RE.search(html):
            return PageType.PRODUCT_DETAIL
        if _ITEMLIST_LD_RE.search(html):
            return PageType.CATEGORY_LISTING

        if use_llm and self.llm.enabled:
            label = self.llm.classify_page(html)
            try:
                return PageType(label)
            except ValueError:
                pass

        return PageType.IRREGULAR
