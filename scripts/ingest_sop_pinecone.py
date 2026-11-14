"""Backward-compatible entrypoint. The logic now lives in the ``rag`` package:

    python scripts/ingest_sop_pinecone.py [--dry-run | --full-rebuild | ...]
    # equivalent to
    python -m rag.ingestion [...]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rag.ingestion.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())