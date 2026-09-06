"""Risk management CLI commands."""

from structlog import get_logger

from hft_platform.risk import kill_switch

logger = get_logger(__name__)

# The latch itself lives in hft_platform.risk.kill_switch so the engine's own
# startup gate and this operator surface cannot drift on path or record shape.
_DEFAULT_KILL_SWITCH_PATH = kill_switch.DEFAULT_PATH
_get_kill_switch_path = kill_switch.kill_switch_path


def cmd_risk_halt(args):
    path = kill_switch.activate(args.reason, actor="cli")
    print(f"Kill switch ACTIVATED at {path}")
    print(f"Reason: {args.reason}")


def cmd_risk_resume(args):
    path = _get_kill_switch_path()
    if kill_switch.deactivate(path):
        print(f"Kill switch DEACTIVATED (removed {path})")
    else:
        print(f"No kill switch file found at {path}")


def cmd_risk_status(args):
    path = _get_kill_switch_path()
    if kill_switch.is_active(path):
        try:
            data = kill_switch.read_payload(path)
            print("Status: ACTIVE")
            print(f"Reason: {data.get('reason', 'unknown')}")
            print(f"Actor:  {data.get('actor', 'unknown')}")
            print(f"Time:   {data.get('timestamp_ns', 0)}")
        except Exception:
            print("Status: ACTIVE (file corrupt)")
    else:
        print("Status: INACTIVE")
