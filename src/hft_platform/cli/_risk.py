"""Risk management CLI commands."""

import json
import os

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger(__name__)

_DEFAULT_KILL_SWITCH_PATH = ".runtime/kill_switch"


def _get_kill_switch_path() -> str:
    return os.getenv("HFT_KILL_SWITCH_PATH", _DEFAULT_KILL_SWITCH_PATH)


def cmd_risk_halt(args):
    path = _get_kill_switch_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    data = {"reason": args.reason, "timestamp_ns": timebase.now_ns(), "actor": "cli"}
    with open(path, "w") as f:
        json.dump(data, f)
    print(f"Kill switch ACTIVATED at {path}")
    print(f"Reason: {args.reason}")


def cmd_risk_resume(args):
    path = _get_kill_switch_path()
    if os.path.exists(path):
        os.remove(path)
        print(f"Kill switch DEACTIVATED (removed {path})")
    else:
        print(f"No kill switch file found at {path}")


def cmd_risk_status(args):
    path = _get_kill_switch_path()
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
            print("Status: ACTIVE")
            print(f"Reason: {data.get('reason', 'unknown')}")
            print(f"Actor:  {data.get('actor', 'unknown')}")
            print(f"Time:   {data.get('timestamp_ns', 0)}")
        except Exception:
            print("Status: ACTIVE (file corrupt)")
    else:
        print("Status: INACTIVE")
