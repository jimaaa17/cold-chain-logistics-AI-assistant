"""Ingestion service entrypoint.

    python -m rag.ingestion                  # incremental sync of SOP_SOURCE_DIR into Pinecone
    python -m rag.ingestion --dry-run        # show the plan; loads no model, touches no index
    python -m rag.ingestion --full-rebuild   # re-embed everything (e.g. after a model change)

Exit codes: 0 = success, 1 = some documents failed (retried next run), 2 = configuration error.
Designed to run as a scheduled job (cron, ECS scheduled task, K8s CronJob) with concurrency 1.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from rag.config import PROJECT_ROOT, configure_logging, get_settings
from rag.core.vector_store import IndexDimensionMismatch, IndexNotFound

logger = logging.getLogger("rag.ingestion")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Incrementally sync SOP/policy documents into Pinecone.")
    parser.add_argument("--source-dir", type=Path, help="Override SOP_SOURCE_DIR")
    parser.add_argument("--full-rebuild", action="store_true", help="Re-embed every document")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan without writing anything")
    parser.add_argument("--workers", type=int, help="Override INGEST_WORKERS")
    parser.add_argument("--allow-index-recreate", action="store_true",
                        help="Allow dropping the index if its dimension doesn't match the model")
    args = parser.parse_args(argv)

    settings = get_settings()
    overrides: dict = {}
    if args.source_dir:
        source = args.source_dir if args.source_dir.is_absolute() else (PROJECT_ROOT / args.source_dir)
        overrides["sop_source_dir"] = source.resolve()
    if args.workers:
        overrides["ingest_workers"] = args.workers
    if args.allow_index_recreate:
        overrides["allow_index_recreate"] = True
    settings = settings.model_copy(update=overrides)
    configure_logging(settings.rag_log_level)

    # Heavy imports deferred so --help / --dry-run stay instant.
    from rag.ingestion.manifest import Manifest
    from rag.ingestion.pipeline import IngestionPipeline, build_plan

    manifest = Manifest(settings.manifest_path, settings.pipeline_fingerprint())
    try:
        plan = build_plan(settings, manifest, full_rebuild=args.full_rebuild)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 2

    logger.info(
        "Index=%s namespace=%s | %d file(s): %d to ingest, %d unchanged, %d to purge",
        settings.index_name, settings.pinecone_namespace,
        len(plan.files), len(plan.to_process), len(plan.unchanged), len(plan.to_delete),
    )
    for name in plan.to_process:
        logger.info("  ~ ingest  %s", name)
    for name in plan.to_delete:
        logger.info("  - purge   %s", name)
    if args.dry_run:
        return 0
    if not plan.to_process and not plan.to_delete and manifest.legacy_migrated:
        logger.info("Index is already up to date.")
        return 0

    from rag.core.embeddings import build_embedder
    from rag.core.vector_store import connect_vector_store

    try:
        embedder = build_embedder(settings)
        store = connect_vector_store(settings, embedder.dimension, provision=True)
    except (IndexDimensionMismatch, IndexNotFound) as exc:
        logger.error("%s", exc)
        return 2

    report = IngestionPipeline(settings, embedder, store, manifest).run(plan)
    logger.info(
        "Done in %.2fs: %d ingested (%d chunks, %d stale pruned), %d unchanged, %d purged, %d failed",
        report.duration_s, len(report.processed), report.chunks_upserted, report.stale_chunks_pruned,
        len(report.skipped), len(report.deleted), len(report.failed),
    )
    for name, error in report.failed.items():
        logger.error("  ! %s: %s", name, error)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

