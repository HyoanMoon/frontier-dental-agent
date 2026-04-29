"""Smoke tests for the validator + dedup logic.

Run with: PYTHONPATH=. pytest tests/
"""

from __future__ import annotations

from src.agents.validator import ValidatorAgent
from src.core.schema import Price, Product, StockStatus


def _p(sku: str, **overrides) -> Product:
    base = dict(
        name="Test glove",
        sku=sku,
        url="https://www.safcodental.com/product/test",
        manufacturer="ACME",
        categories=["Dental Supplies", "Dental Exam Gloves"],
        price=Price(amount=9.99, currency="USD"),
        stock=StockStatus.IN_STOCK,
        description="A glove.",
    )
    base.update(overrides)
    return Product(**base)


def test_dedup_merges_duplicate_skus():
    v = ValidatorAgent()
    p1 = _p("ABC-1", description=None)
    p2 = _p("ABC-1", description="Filled in by a later layer")
    out = v.validate([p1, p2])
    assert len(out) == 1
    assert out[0].description == "Filled in by a later layer"
    assert v.report.total == 1


def test_unique_skus_pass_through():
    v = ValidatorAgent()
    out = v.validate([_p("ABC-1"), _p("ABC-2"), _p("ABC-3")])
    assert {p.sku for p in out} == {"ABC-1", "ABC-2", "ABC-3"}
    assert v.report.failed_validation == 0


def test_quality_report_tracks_missing_fields():
    v = ValidatorAgent()
    v.validate([_p("X1", description=None, manufacturer=None)])
    assert v.report.missing.get("description") == 1
    assert v.report.missing.get("manufacturer") == 1
