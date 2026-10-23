"""Risk management CLI commands."""

import getpass
import sys

from structlog import get_logger

from hft_platform.risk import kill_switch, manual_latches

logger = get_logger(__name__)

# The latch itself lives in hft_platform.risk.kill_switch so the engine's own
# startup gate and this operator surface cannot drift on path or record shape.
_DEFAULT_KILL_SWITCH_PATH = kill_switch.DEFAULT_PATH
_get_kill_switch_path = kill_switch.kill_switch_path


def cmd_risk_halt(args):
    path = kill_switch.activate(args.reason, actor="cli")
    print(f"Kill switch ACTIVATED at {path}")
    print(f"Reason: {args.reason}")


def _actor() -> str:
    return f"cli:{getpass.getuser()}"


def cmd_risk_resume(args):
    """Release the kill switch. Must quote the token ``hft risk status`` printed."""
    path = kill_switch.active_path() or _get_kill_switch_path()
    # No explicit path: a latch left at the legacy location is removed too.
    try:
        released = kill_switch.release(token=args.token, reason=args.reason, actor=_actor())
    except ValueError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if released:
        print(f"Kill switch DEACTIVATED (removed {path})")
        print("The engine applies it on its next supervise tick; HALT then de-escalates through its cooldown.")
    else:
        print(f"No kill switch file found at {path}")


def cmd_risk_status(args):
    path = kill_switch.active_path()
    if path is not None:
        try:
            data = kill_switch.read_payload(path)
            print("Status: ACTIVE")
            print(f"Path:   {path}")
            print(f"Reason: {data.get('reason', 'unknown')}")
            print(f"Actor:  {data.get('actor', 'unknown')}")
            print(f"Time:   {data.get('timestamp_ns', 0)}")
        except Exception:
            print("Status: ACTIVE (file corrupt)")
        print(f"Token:  {kill_switch.current_token()}  (hft risk resume --token ... --reason ...)")
    else:
        print("Status: INACTIVE")
    _print_latches()


def _print_latches() -> None:
    latches = manual_latches.active()
    if not latches:
        print("Manual latches: none")
        return
    tokens = manual_latches.tokens()
    print(f"Manual latches ({manual_latches.latch_dir()}):")
    for name, reason in sorted(latches.items()):
        print(f"  {name}: {reason}  token={tokens.get(name, '?')}")


def cmd_risk_latches(args):
    _print_latches()


def cmd_risk_rearm(args):
    """Release one durable manual latch. The engine sees it on its next supervise tick."""
    try:
        released = manual_latches.release(args.latch, token=args.token, reason=args.reason, actor=_actor())
    except ValueError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if released:
        print(f"Manual latch {args.latch} RELEASED (audit: {manual_latches.latch_dir() / manual_latches.AUDIT_FILE})")
        print("The engine applies it on its next supervise tick; no restart is needed.")
    else:
        print(f"No manual latch {args.latch} found in {manual_latches.latch_dir()}")
