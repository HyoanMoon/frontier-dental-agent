"""CLI entry point.

Subcommands:

    run      — full pipeline: discover, list, enrich, validate, persist, export
    export   — re-export from an existing SQLite database (no network calls)
    info     — print row count and last-run summary
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from src.core.config import load_config
from src.core.logger import bind_run_id, configure_logging, get_logger, new_run_id
from src.core.store import open_store
from src.orchestrator import Orchestrator


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    configure_logging(cfg.logging.level, cfg.logging.format, cfg.logging.file)

    run_id = args.run_id or new_run_id()
    bind_run_id(run_id)
    log = get_logger("main")
    log.info("run_starting", run_id=run_id, max_per_category=args.max_per_category)

    with open_store(cfg.storage.sqlite_path) as store:
        orch = Orchestrator(cfg, run_id, store)
        summary = orch.run(
            enrich=not args.no_enrich,
            max_per_category=args.max_per_category,
        )
        log.info("run_completed", **summary.__dict__)

        # Exports
        if "json" in cfg.storage.exports:
            n = store.export_json(cfg.storage.exports["json"])
            log.info("exported_json", path=cfg.storage.exports["json"], rows=n)
        if "csv" in cfg.storage.exports:
            n = store.export_csv(cfg.storage.exports["csv"])
            log.info("exported_csv", path=cfg.storage.exports["csv"], rows=n)

    return 0


def cmd_export(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    configure_logging(cfg.logging.level, cfg.logging.format, cfg.logging.file)
    log = get_logger("main")

    with open_store(cfg.storage.sqlite_path) as store:
        if args.format in ("json", "all") and "json" in cfg.storage.exports:
            n = store.export_json(cfg.storage.exports["json"])
            log.info("exported_json", path=cfg.storage.exports["json"], rows=n)
        if args.format in ("csv", "all") and "csv" in cfg.storage.exports:
            n = store.export_csv(cfg.storage.exports["csv"])
            log.info("exported_csv", path=cfg.storage.exports["csv"], rows=n)
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    configure_logging("INFO", "console", None)
    with open_store(cfg.storage.sqlite_path) as store:
        n = store.count_products()
        print(f"products in {cfg.storage.sqlite_path}: {n}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="frontier-dental-agent")
    p.add_argument("--config", default="config/config.yaml")

    sub = p.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Run the full scraping pipeline")
    run_p.add_argument("--run-id", default=None, help="Resume an interrupted run")
    run_p.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip product-page enrichment (Layer 2/3/4); use Algolia data only",
    )
    run_p.add_argument(
        "--max-per-category",
        type=int,
        default=None,
        help="Cap products per category (smoke testing).",
    )
    run_p.set_defaults(func=cmd_run)

    exp_p = sub.add_parser("export", help="Re-export from existing DB")
    exp_p.add_argument("--format", choices=("json", "csv", "all"), default="all")
    exp_p.set_defaults(func=cmd_export)

    info_p = sub.add_parser("info", help="Show DB row count")
    info_p.set_defaults(func=cmd_info)

    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
