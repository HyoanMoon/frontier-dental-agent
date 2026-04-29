"""Anthropic LLM wrapper for the Layer-4 fallback.

Two design rules:
1. Only used when prior layers have failed for a specific field.
2. Hard-capped per run (``max_calls_per_run``) so a buggy loop can't burn money.

The wrapper degrades gracefully: if no API key is present, ``LLMClient.enabled``
is False and every method returns None instead of raising. Callers must check
``enabled`` (or accept the None) and fall back to "field unknown".
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.core.config import LLMConfig, anthropic_api_key
from src.core.logger import get_logger

log = get_logger(__name__)


class LLMClient:
    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self.calls_made = 0
        self._client = None

        api_key = anthropic_api_key()
        if cfg.enabled and api_key:
            from anthropic import Anthropic

            self._client = Anthropic(api_key=api_key)
            log.info("llm_enabled", model=cfg.model, cap=cfg.max_calls_per_run)
        else:
            log.info(
                "llm_disabled",
                reason="no_api_key" if not api_key else "config_disabled",
            )

    @property
    def enabled(self) -> bool:
        return self._client is not None

    # ── budget guard ────────────────────────────────────────────────────

    def _can_call(self) -> bool:
        if not self.enabled:
            return False
        if self.calls_made >= self.cfg.max_calls_per_run:
            log.warning("llm_budget_exhausted", calls=self.calls_made)
            return False
        return True

    # ── primitive ───────────────────────────────────────────────────────

    def _ask_json(self, system: str, user: str) -> dict[str, Any] | None:
        if not self._can_call():
            return None
        try:
            self.calls_made += 1
            resp = self._client.messages.create(  # type: ignore[union-attr]
                model=self.cfg.model,
                max_tokens=512,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            return _parse_json_loose(text)
        except Exception as e:  # noqa: BLE001 — LLM failure is non-fatal
            log.warning("llm_call_failed", error=str(e))
            return None

    # ── high-level helpers ──────────────────────────────────────────────

    def extract_pack_size(self, product_name: str, description: str | None) -> str | None:
        """Pull the unit/pack size out of a product name or description.

        Examples seen in the corpus:
            "Aquasoft x-large 200/box" -> "200/box"
            "Alasta gloves medium 100ct" -> "100ct"
            "Sutures 12/box" -> "12/box"
        """
        system = (
            "You extract the pack-size or unit count from a dental-product name "
            "or description. Reply ONLY with JSON of the form "
            '{"pack_size": "<value>"} or {"pack_size": null}. '
            "Examples of valid pack_size values: '200/box', '100/case', '12/pkg', "
            "'50ct', '24 boxes/case'. If unsure, return null."
        )
        user = f"Product name: {product_name}\nDescription: {description or '(none)'}\n"
        out = self._ask_json(system, user)
        if not out:
            return None
        val = out.get("pack_size")
        return val if isinstance(val, str) and val.strip() else None

    def extract_specifications(self, html_excerpt: str) -> dict[str, str]:
        """Pull a key/value spec table out of an irregular product page."""
        excerpt = html_excerpt[: self.cfg.max_input_chars]
        system = (
            "You extract product specifications from raw HTML. Reply ONLY with "
            'a JSON object: {"specifications": {"key": "value", ...}}. Keys must '
            "be human-readable attribute names (e.g. 'Material', 'Color', "
            "'Size', 'Sterile'). Skip pricing, SKU, brand, and description. "
            "Return {} if no spec table is present."
        )
        out = self._ask_json(system, f"HTML:\n{excerpt}")
        if not out:
            return {}
        specs = out.get("specifications") or {}
        return {str(k): str(v) for k, v in specs.items() if v}

    def classify_page(self, html_excerpt: str) -> str:
        """Return one of: 'product_detail' | 'category_listing' | 'irregular'."""
        excerpt = html_excerpt[: self.cfg.max_input_chars]
        system = (
            "Classify the page. Reply ONLY with JSON: "
            '{"page_type": "product_detail" | "category_listing" | "irregular"}.'
        )
        out = self._ask_json(system, f"HTML:\n{excerpt}")
        if not out:
            return "irregular"
        return str(out.get("page_type", "irregular"))


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.S)


def _parse_json_loose(text: str) -> dict[str, Any] | None:
    """Tolerate the model wrapping JSON in prose or fences."""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_BLOCK_RE.search(text)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
