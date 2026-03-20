# cross_ofi_network

`cross_ofi_network` extends the single-leader OFI idea into an N-leader network with
adaptive correlation-based weights. The alpha blends self OFI with multiple leader
OFI streams while capping aggregate cross weight to preserve self-signal dominance.

Primary implementation lives in `impl.py`. Tests live under `tests/`.

Research intent:
- capture sector and market-wide information flow via multiple leaders
- refresh leader weights from rolling correlation estimates
- degrade safely to pure self-OFI when leader inputs are missing

Reference paper:
- `2112.13213`
