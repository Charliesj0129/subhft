"""Tests for YAML-driven healing playbook."""
from __future__ import annotations

from pathlib import Path

import yaml

from hft_platform.healing.fault import FaultCategory, FaultEvent, FaultSeverity


def _make_fault(
    *,
    category: FaultCategory = FaultCategory.FEED,
    severity: FaultSeverity = FaultSeverity.DEGRADED,
    description: str = "feed_gap",
    context: dict | None = None,
    ts_ns: int = 1_000_000_000_000_000_000,
) -> FaultEvent:
    return FaultEvent(
        fault_id="f-001", category=category, severity=severity,
        source="test", description=description,
        ts_ns=ts_ns, context=context or {},
    )


class TestHealingPlaybook:
    def test_load_from_yaml(self, tmp_path):
        from hft_platform.healing.playbook import HealingPlaybook
        config = {"playbooks": {"feed_gap_short": {
            "match": {"category": "feed", "description_contains": "feed_gap"},
            "actions": [{"name": "resubscribe_symbol", "risk": "auto", "timeout_s": 10}],
            "cooldown_s": 60, "max_retries": 3,
        }}}
        path = tmp_path / "playbook.yaml"
        path.write_text(yaml.dump(config))
        playbook = HealingPlaybook(path)
        assert len(playbook._playbooks) == 1

    def test_match_by_category_and_description(self, tmp_path):
        from hft_platform.healing.playbook import HealingPlaybook
        config = {"playbooks": {"feed_gap_short": {
            "match": {"category": "feed", "description_contains": "feed_gap"},
            "actions": [{"name": "resubscribe_symbol", "risk": "auto", "timeout_s": 10}],
            "cooldown_s": 60, "max_retries": 3,
        }}}
        path = tmp_path / "playbook.yaml"
        path.write_text(yaml.dump(config))
        playbook = HealingPlaybook(path)
        fault = _make_fault(description="feed_gap detected on TMFD6")
        entry = playbook.find_match(fault)
        assert entry is not None
        assert entry.name == "feed_gap_short"

    def test_no_match_returns_none(self, tmp_path):
        from hft_platform.healing.playbook import HealingPlaybook
        config = {"playbooks": {"feed_gap_short": {
            "match": {"category": "feed", "description_contains": "feed_gap"},
            "actions": [{"name": "resubscribe_symbol", "risk": "auto", "timeout_s": 10}],
            "cooldown_s": 60, "max_retries": 3,
        }}}
        path = tmp_path / "playbook.yaml"
        path.write_text(yaml.dump(config))
        playbook = HealingPlaybook(path)
        fault = _make_fault(category=FaultCategory.BROKER, description="broker disconnected")
        assert playbook.find_match(fault) is None

    def test_cooldown_prevents_rematch(self, tmp_path):
        from hft_platform.healing.playbook import HealingPlaybook
        config = {"playbooks": {"feed_gap_short": {
            "match": {"category": "feed", "description_contains": "feed_gap"},
            "actions": [{"name": "resubscribe_symbol", "risk": "auto", "timeout_s": 10}],
            "cooldown_s": 60, "max_retries": 3,
        }}}
        path = tmp_path / "playbook.yaml"
        path.write_text(yaml.dump(config))
        playbook = HealingPlaybook(path)
        fault = _make_fault(ts_ns=1_000_000_000_000_000_000)
        assert playbook.find_match(fault) is not None
        playbook.mark_used("feed_gap_short", ts_ns=1_000_000_000_000_000_000)
        fault2 = _make_fault(description="feed_gap again", ts_ns=1_000_000_000_000_000_000 + 30_000_000_000)
        assert playbook.find_match(fault2) is None  # within cooldown

    def test_multiple_playbooks_severity_gated(self, tmp_path):
        from hft_platform.healing.playbook import HealingPlaybook
        config = {"playbooks": {
            "feed_gap_short": {
                "match": {"category": "feed", "description_contains": "feed_gap"},
                "actions": [{"name": "resubscribe_symbol", "risk": "auto", "timeout_s": 10}],
                "cooldown_s": 60, "max_retries": 3,
            },
            "feed_gap_long": {
                "match": {"category": "feed", "description_contains": "feed_gap", "min_severity": "impaired"},
                "actions": [{"name": "relogin_broker", "risk": "auto", "timeout_s": 30}],
                "cooldown_s": 300, "max_retries": 1,
            },
        }}
        path = tmp_path / "playbook.yaml"
        path.write_text(yaml.dump(config))
        playbook = HealingPlaybook(path)
        # Low severity matches first playbook
        fault = _make_fault(severity=FaultSeverity.DEGRADED)
        entry = playbook.find_match(fault)
        assert entry is not None
        assert entry.name == "feed_gap_short"
        # Mark first used, high severity matches second
        playbook.mark_used("feed_gap_short", ts_ns=fault.ts_ns)
        fault_high = FaultEvent(
            fault_id="f-003", category=FaultCategory.FEED,
            severity=FaultSeverity.IMPAIRED, source="test",
            description="feed_gap long outage",
            ts_ns=fault.ts_ns + 10_000_000_000, context={},
        )
        entry2 = playbook.find_match(fault_high)
        assert entry2 is not None
        assert entry2.name == "feed_gap_long"

    def test_load_nonexistent_path_does_not_raise(self):
        from hft_platform.healing.playbook import HealingPlaybook
        playbook = HealingPlaybook(Path("/tmp/does_not_exist_xyz.yaml"))
        assert playbook._playbooks == []

    def test_load_none_path(self):
        from hft_platform.healing.playbook import HealingPlaybook
        playbook = HealingPlaybook(None)
        assert playbook._playbooks == []

    def test_action_fields_parsed(self, tmp_path):
        from hft_platform.healing.fault import RiskLevel
        from hft_platform.healing.playbook import HealingPlaybook, PlaybookAction
        config = {"playbooks": {"my_playbook": {
            "match": {"category": "broker"},
            "actions": [
                {"name": "relogin_broker", "risk": "auto", "timeout_s": 30, "params": {"retries": 3}},
                {"name": "alert_and_wait", "risk": "confirm", "timeout_s": 900},
            ],
            "cooldown_s": 120, "max_retries": 2,
        }}}
        path = tmp_path / "playbook.yaml"
        path.write_text(yaml.dump(config))
        playbook = HealingPlaybook(path)
        entry = playbook._playbooks[0]
        assert len(entry.actions) == 2
        a0: PlaybookAction = entry.actions[0]
        assert a0.name == "relogin_broker"
        assert a0.risk == RiskLevel.AUTO
        assert a0.timeout_s == 30.0
        assert a0.params == {"retries": 3}
        a1: PlaybookAction = entry.actions[1]
        assert a1.risk == RiskLevel.CONFIRM

    def test_cooldown_expired_allows_rematch(self, tmp_path):
        from hft_platform.healing.playbook import HealingPlaybook
        config = {"playbooks": {"feed_gap_short": {
            "match": {"category": "feed", "description_contains": "feed_gap"},
            "actions": [{"name": "resubscribe_symbol", "risk": "auto", "timeout_s": 10}],
            "cooldown_s": 60, "max_retries": 3,
        }}}
        path = tmp_path / "playbook.yaml"
        path.write_text(yaml.dump(config))
        playbook = HealingPlaybook(path)
        base_ts = 1_000_000_000_000_000_000
        playbook.mark_used("feed_gap_short", ts_ns=base_ts)
        # 61 seconds later — past cooldown
        fault_after = _make_fault(
            description="feed_gap again",
            ts_ns=base_ts + 61_000_000_000,
        )
        entry = playbook.find_match(fault_after)
        assert entry is not None
        assert entry.name == "feed_gap_short"

    def test_match_category_only_no_description_filter(self, tmp_path):
        from hft_platform.healing.playbook import HealingPlaybook
        config = {"playbooks": {"broker_any": {
            "match": {"category": "broker"},
            "actions": [{"name": "relogin_broker", "risk": "auto", "timeout_s": 30}],
            "cooldown_s": 120, "max_retries": 3,
        }}}
        path = tmp_path / "playbook.yaml"
        path.write_text(yaml.dump(config))
        playbook = HealingPlaybook(path)
        fault = _make_fault(category=FaultCategory.BROKER, description="some unknown broker error")
        entry = playbook.find_match(fault)
        assert entry is not None
        assert entry.name == "broker_any"

    def test_mark_used_updates_last_used(self, tmp_path):
        from hft_platform.healing.playbook import HealingPlaybook
        config = {"playbooks": {"feed_gap_short": {
            "match": {"category": "feed", "description_contains": "feed_gap"},
            "actions": [{"name": "resubscribe_symbol", "risk": "auto", "timeout_s": 10}],
            "cooldown_s": 60, "max_retries": 3,
        }}}
        path = tmp_path / "playbook.yaml"
        path.write_text(yaml.dump(config))
        playbook = HealingPlaybook(path)
        ts = 5_000_000_000_000_000_000
        playbook.mark_used("feed_gap_short", ts_ns=ts)
        assert playbook._last_used_ns["feed_gap_short"] == ts

    def test_canonical_config_loads(self):
        """Verify the shipped config/base/healing_playbook.yaml loads without error."""
        from hft_platform.healing.playbook import HealingPlaybook
        config_path = Path("/home/charlie/hft_platform/config/base/healing_playbook.yaml")
        playbook = HealingPlaybook(config_path)
        assert len(playbook._playbooks) == 10  # 10 entries in canonical config
