import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

for candidate in (ROOT, SRC):
    path = str(candidate)
    if path not in sys.path:
        sys.path.insert(0, path)
