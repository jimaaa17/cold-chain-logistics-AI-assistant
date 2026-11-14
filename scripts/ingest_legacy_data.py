"""Backward-compatible entrypoint. The logic now lives in the ``legacy_ingest`` package:

    python scripts/ingest_legacy_data.py [--mode upsert | --dry-run | --force | ...]
    # equivalent to
    python -m legacy_ingest [...]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from legacy_ingest.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
