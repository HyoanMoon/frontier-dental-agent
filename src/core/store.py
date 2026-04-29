"""SQLite-backed persistent store with idempotent UPSERT.

Two tables:
- ``products``       — canonical Product rows keyed on SKU.
- ``crawl_state``    — checkpoint table; one row per (run_id, category, page).

Exports (JSON, CSV) are derived views written from a SELECT, so the database is
always the source of truth and an interrupted export never corrupts anything.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from src.core.logger import get_logger
from src.core.schema import Product

log = get_logger(__name__)


_DDL = [
    """
    CREATE TABLE IF NOT EXISTS products (
        sku TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        url TEXT NOT NULL,
        manufacturer TEXT,
        categories TEXT,        -- JSON array
        price_amount REAL,
        price_currency TEXT,
        price_formatted TEXT,
        pack_size TEXT,
        stock TEXT,
        description TEXT,
        specifications TEXT,    -- JSON object
        images TEXT,            -- JSON array
        alternatives TEXT,      -- JSON array
        additional_skus TEXT,   -- JSON array
        source_layer TEXT,      -- JSON object
        scraped_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS crawl_state (
        run_id TEXT NOT NULL,
        category TEXT NOT NULL,
        page INTEGER NOT NULL,
        status TEXT NOT NULL,       -- pending | in_progress | done | failed
        last_url TEXT,
        error TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (run_id, category, page)
    );
    """,
]


class Store:
    def __init__(self, sqlite_path: str) -> None:
        Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        self.path = sqlite_path
        self.conn = sqlite3.connect(sqlite_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        for stmt in _DDL:
            self.conn.execute(stmt)
        self.conn.commit()

    # ── product upsert ──────────────────────────────────────────────────

    def upsert_product(self, p: Product) -> None:
        now = datetime.utcnow().isoformat()
        row = {
            "sku": p.sku,
            "name": p.name,
            "url": str(p.url),
            "manufacturer": p.manufacturer,
            "categories": json.dumps(p.categories, ensure_ascii=False),
            "price_amount": p.price.amount if p.price else None,
            "price_currency": p.price.currency if p.price else None,
            "price_formatted": p.price.formatted if p.price else None,
            "pack_size": p.pack_size,
            "stock": p.stock if isinstance(p.stock, str) else p.stock.value,
            "description": p.description,
            "specifications": json.dumps(p.specifications, ensure_ascii=False),
            "images": json.dumps(p.images, ensure_ascii=False),
            "alternatives": json.dumps(
                [a.model_dump() for a in p.alternatives], ensure_ascii=False
            ),
            "additional_skus": json.dumps(p.additional_skus, ensure_ascii=False),
            "source_layer": json.dumps(p.source_layer, ensure_ascii=False),
            "scraped_at": p.scraped_at.isoformat(),
            "last_seen_at": now,
        }
        self.conn.execute(
            """
            INSERT INTO products (
                sku, name, url, manufacturer, categories,
                price_amount, price_currency, price_formatted,
                pack_size, stock, description, specifications,
                images, alternatives, additional_skus,
                source_layer, scraped_at, last_seen_at
            ) VALUES (
                :sku, :name, :url, :manufacturer, :categories,
                :price_amount, :price_currency, :price_formatted,
                :pack_size, :stock, :description, :specifications,
                :images, :alternatives, :additional_skus,
                :source_layer, :scraped_at, :last_seen_at
            )
            ON CONFLICT(sku) DO UPDATE SET
                name=excluded.name,
                url=excluded.url,
                manufacturer=excluded.manufacturer,
                categories=excluded.categories,
                price_amount=excluded.price_amount,
                price_currency=excluded.price_currency,
                price_formatted=excluded.price_formatted,
                pack_size=excluded.pack_size,
                stock=excluded.stock,
                description=excluded.description,
                specifications=excluded.specifications,
                images=excluded.images,
                alternatives=excluded.alternatives,
                additional_skus=excluded.additional_skus,
                source_layer=excluded.source_layer,
                scraped_at=excluded.scraped_at,
                last_seen_at=excluded.last_seen_at
            """,
            row,
        )
        self.conn.commit()

    def count_products(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]

    # ── crawl state ─────────────────────────────────────────────────────

    def set_state(
        self,
        run_id: str,
        category: str,
        page: int,
        status: str,
        last_url: str | None = None,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO crawl_state (run_id, category, page, status, last_url, error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, category, page) DO UPDATE SET
                status=excluded.status,
                last_url=excluded.last_url,
                error=excluded.error,
                updated_at=excluded.updated_at
            """,
            (
                run_id,
                category,
                page,
                status,
                last_url,
                error,
                datetime.utcnow().isoformat(),
            ),
        )
        self.conn.commit()

    def pending_state(self, run_id: str) -> list[tuple[str, int]]:
        """List ``(category, page)`` rows that are not yet ``done``.

        Reserved for a per-product checkpoint extension: today the
        orchestrator only consults :meth:`done_categories` (category-level
        resume), so this method is unused inside the prototype but is kept
        as a public API so a future per-product resume can plug in without
        a schema change. See README *Limitations → Resumability granularity*.
        """
        rows = self.conn.execute(
            """
            SELECT category, page FROM crawl_state
            WHERE run_id = ? AND status IN ('pending', 'in_progress', 'failed')
            ORDER BY category, page
            """,
            (run_id,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def done_categories(self, run_id: str) -> set[str]:
        """Return the names of categories already marked ``done`` for this run.

        Used by the orchestrator on resume: any category in this set has
        already been collected and persisted in a previous invocation of
        the same ``run_id``, so we skip the (potentially expensive) Algolia
        fetch loop and rely on the existing ``products`` rows.
        """
        rows = self.conn.execute(
            """
            SELECT DISTINCT category FROM crawl_state
            WHERE run_id = ? AND status = 'done'
            """,
            (run_id,),
        ).fetchall()
        return {r[0] for r in rows}

    # ── exports ─────────────────────────────────────────────────────────

    def _iter_rows(self) -> Iterator[dict]:
        cur = self.conn.execute("SELECT * FROM products ORDER BY sku")
        cols = [c[0] for c in cur.description]
        for row in cur.fetchall():
            yield dict(zip(cols, row))

    def export_json(self, path: str) -> int:
        rows = []
        for r in self._iter_rows():
            for k in (
                "categories",
                "specifications",
                "images",
                "alternatives",
                "additional_skus",
                "source_layer",
            ):
                if r.get(k):
                    r[k] = json.loads(r[k])
            rows.append(r)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2, ensure_ascii=False)
        return len(rows)

    def export_csv(self, path: str) -> int:
        """CSV needs flat columns; nested fields are collapsed to delimited strings."""
        rows = list(self._iter_rows())
        if not rows:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("")
            return 0

        # Flatten complex JSON columns to readable strings for CSV consumers.
        for r in rows:
            for k in ("categories", "images", "additional_skus"):
                if r.get(k):
                    try:
                        r[k] = " | ".join(json.loads(r[k]))
                    except (TypeError, json.JSONDecodeError):
                        pass
            for k in ("specifications", "alternatives", "source_layer"):
                if r.get(k):
                    # keep these as JSON strings for round-trippability
                    pass

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return len(rows)

    # ── teardown ───────────────────────────────────────────────────────

    def close(self) -> None:
        self.conn.close()


@contextmanager
def open_store(sqlite_path: str):
    s = Store(sqlite_path)
    try:
        yield s
    finally:
        s.close()
