#!/usr/bin/env python3
"""Send a test alert to alertmanager and verify delivery."""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

STATUS_PASS = "pass"
STATUS_FAIL = "fail"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _send_test_alert(alertmanager_url: str) -> bool:
    """POST a test alert to alertmanager /api/v2/alerts."""
    now = datetime.now(timezone.utc)
    ends_at = now + timedelta(minutes=1)

    alert = [
        {
            "labels": {
                "alertname": "HFT_AlertTest",
                "severity": "info",
                "source": "alert_test_script",
            },
            "annotations": {
                "summary": "Automated alert test from HFT platform",
            },
            "startsAt": now.isoformat(),
            "endsAt": ends_at.isoformat(),
        }
    ]

    url = alertmanager_url.rstrip("/") + "/api/v2/alerts"
    data = json.dumps(alert).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                print(f"[alert-test] POST {url} -> 200 OK")
                return True
            print(f"[alert-test] POST {url} -> {resp.status}")
            return False
    except Exception as exc:
        print(f"[alert-test] POST {url} -> ERROR: {exc}")
        return False


def _verify_alert(alertmanager_url: str) -> bool:
    """GET /api/v2/alerts and check for HFT_AlertTest."""
    url = alertmanager_url.rstrip("/") + "/api/v2/alerts"
    req = urllib.request.Request(url, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"[alert-test] GET {url} -> ERROR: {exc}")
        return False

    if not isinstance(payload, list):
        print("[alert-test] Unexpected response format (not a list)")
        return False

    for alert in payload:
        labels = alert.get("labels", {})
        if labels.get("alertname") == "HFT_AlertTest":
            print("[alert-test] FOUND HFT_AlertTest in active alerts")
            return True

    print(f"[alert-test] HFT_AlertTest NOT found in {len(payload)} active alerts")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Test alertmanager alert delivery")
    parser.add_argument(
        "--alertmanager-url",
        default="http://localhost:9093",
        help="Alertmanager base URL (default: http://localhost:9093)",
    )
    args = parser.parse_args(argv)

    print(f"[alert-test] Target: {args.alertmanager_url}")
    print(f"[alert-test] Time: {_now_iso()}")
    print()

    # Step 1: Send test alert
    print("[alert-test] Step 1: Sending test alert...")
    sent = _send_test_alert(args.alertmanager_url)
    if not sent:
        print(f"\n[alert-test] RESULT: {STATUS_FAIL} — could not send test alert")
        return 1

    # Step 2: Wait for propagation
    print("[alert-test] Step 2: Waiting 2s for propagation...")
    time.sleep(2)

    # Step 3: Verify alert
    print("[alert-test] Step 3: Verifying alert delivery...")
    verified = _verify_alert(args.alertmanager_url)

    if verified:
        print(f"\n[alert-test] RESULT: {STATUS_PASS} — alert sent and verified")
        return 0
    else:
        print(f"\n[alert-test] RESULT: {STATUS_FAIL} — alert sent but not found in active alerts")
        return 1


if __name__ == "__main__":
    sys.exit(main())
