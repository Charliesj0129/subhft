#!/usr/bin/env bash
# Exercise the installed HFT wheel without mounting host data or logging into a broker.
set -euo pipefail
if [[ $# -lt 1 || $# -gt 2 || (${2-} != "" && ${2-} != "--broker-read-only") ]]; then
    echo "Usage: bash scripts/test_deployment_image.sh LOCAL_IMAGE [--broker-read-only]" >&2
    exit 2
fi
image=$1
image_id=$(docker image inspect --format '{{.Id}}' "$image")
run=(docker run --rm --pull=never --network none --cap-drop ALL
     --security-opt no-new-privileges --env HFT_ORDER_MODE=disabled
     --env PYTHONPATH= --workdir /tmp)
"${run[@]}" --entrypoint hft "$image_id" --help >/dev/null
"${run[@]}" -i --entrypoint python "$image_id" -I - <<'PY'
import os
from importlib import import_module
from importlib.metadata import distribution
from importlib.util import find_spec
from pathlib import Path
from shutil import which

if os.geteuid() == 0:
    raise SystemExit("runtime image runs as root")
for tool in ("cargo", "rustc"):
    if which(tool):
        raise SystemExit(f"runtime image contains {tool}")
package = distribution("hft-platform")
if not package.files or find_spec("hft_platform.rust_core") is not None:
    raise SystemExit("invalid HFT wheel or retired extension is importable")
for path in package.files:
    if path.name.startswith("rust_core") or path.suffix in {".rs", ".so", ".pyd", ".dylib"}:
        raise SystemExit(f"unexpected project artifact: {path}")
for name in ("feed_adapter.lob_engine", "feature.engine", "risk.engine",
             "execution.positions", "gateway.exposure", "order.adapter"):
    module = import_module(f"hft_platform.{name}")
    installed = package.locate_file("hft_platform").resolve()
    if not Path(module.__file__).resolve().is_relative_to(installed):
        raise SystemExit(f"{name} did not load from the installed wheel")
print("PASS: installed HFT wheel, Python runtime imports and offline CLI")
PY

if [[ ${2-} == --broker-read-only ]]; then
    : "${SHIOAJI_API_KEY:?Missing broker API key}" "${SHIOAJI_SECRET_KEY:?Missing broker secret}"
    # Keep SDK output and temporary files inside the disposable container.
    if docker run --rm --pull=never -i --read-only --tmpfs /tmp:mode=1777 \
        --ulimit core=0 --log-driver none --cap-drop ALL --security-opt no-new-privileges \
        --workdir /tmp --env HOME=/tmp --env HFT_ORDER_MODE=disabled \
        --env SHIOAJI_API_KEY --env SHIOAJI_SECRET_KEY \
        --entrypoint timeout "$image_id" --kill-after=5s 60s python -I - >/dev/null 2>&1 <<'PY'
import os
import shioaji

api = shioaji.Shioaji()
try:
    accounts = api.login(os.environ["SHIOAJI_API_KEY"], os.environ["SHIOAJI_SECRET_KEY"],
                         subscribe_trade=False)
    if not accounts or api.usage(timeout=10000) is None:
        raise SystemExit(1)
finally:
    api.logout()
PY
    then
        echo "PASS: single broker login, usage query and logout"
    else
        echo "FAIL: broker connectivity or clean logout could not be verified" >&2
        exit 1
    fi
fi
