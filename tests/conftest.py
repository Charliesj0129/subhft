import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# Add src/ first so compiled hft_platform.rust_core takes priority.
for candidate in (SRC, ROOT):
    path = str(candidate)
    if path not in sys.path:
        sys.path.insert(0, path)

# The rust_core/ directory at ROOT is Cargo source, not a Python package.
# If Python accidentally imported it as a namespace package (before the
# compiled extension was loaded), evict it so the real module can be found.

_rust_ns = sys.modules.get("rust_core")
if _rust_ns is not None and getattr(_rust_ns, "__spec__", None) is not None:
    if getattr(_rust_ns.__spec__, "origin", None) is None:
        # Namespace package — evict so hft_platform.rust_core alias can fill in
        del sys.modules["rust_core"]
