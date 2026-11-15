"""Automatic Python path bootstrap for the BE/ (Backend) monorepo directory.

When Python starts from the repository root, `sitecustomize` is imported automatically
by the standard library `site` module, registering `BE/` on `sys.path` so that
`import agent`, `import rag`, `import legacy_ingest`, and `import api` resolve to `BE/*`.
"""
import sys
from pathlib import Path

_be_dir = str(Path(__file__).resolve().parent / "BE")
if _be_dir not in sys.path:
    sys.path.insert(0, _be_dir)
