"""Test suite bootstrap ensuring BE/ is on sys.path."""
import sys
from pathlib import Path

_be_dir = str(Path(__file__).resolve().parent.parent / "BE")
if _be_dir not in sys.path:
    sys.path.insert(0, _be_dir)
