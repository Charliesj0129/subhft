"""Alpha promotion pipeline — Gate D (backtest) + Gate E (paper-trade) + Gate F (Rust readiness).

``promote_alpha`` is the single entry point.  It:
  1. Loads the backtest scorecard produced by Gate C.
  2. Evaluates Gate D (quantitative thresholds: Sharpe, drawdown, turnover, correlation).
  3. Evaluates Gate E (paper-trade governance: 1-week span, drift, rejection quality).
  4. Evaluates Gate F (Rust readiness: manifest + parity tests [+ optional perf gate]).
  5. Writes ``integration_report.json``, ``paper_governance_report.json``, and
     ``promotion_decision.json`` under experiments promotions.
  6. On approval, writes a YAML promotion config under ``config/strategy_promotions/``.
  7. Best-effort audit logs to ClickHouse (guarded by ``HFT_ALPHA_AUDIT_ENABLED``).
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import shlex
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import structlog
import yaml

from hft_platform.alpha.evidence import EvidenceWriter, GovernedRunRefused, RunMode, is_sealed, require_governed
from hft_platform.alpha.outcomes import Outcome, Verdict, VerdictResult, derive_verdict, resolve_outcome
from hft_platform.alpha.provenance import file_sha256, git_state
from hft_platform.core import timebase

_log = structlog.get_logger(__name__)


class GateCNotPassedError(ValueError):
    """Gate C did not record a PASS for the scorecard being promoted.

    ``verdict`` is the stored Gate C verdict when the run recorded one, and
    ``run_id`` the run it belongs to; promotion records a kill only when that
    verdict is KILL.
    """

    def __init__(self, message: str, *, verdict: VerdictResult | None = None, run_id: str = "") -> None:
        super().__init__(message)
        self.verdict = verdict
        self.run_id = run_id


class PromotionError(RuntimeError):
    """Raised when a promotion is rejected by a structural pre-check."""


@dataclass(frozen=True)
class PromotionConfig:
    alpha_id: str
    owner: str
    project_root: str = "."
    experiments_dir: str = "research/experiments"
    scorecard_path: str | None = None
    shadow_sessions: int = 0
    min_shadow_sessions: int = 5
    drift_alerts: int = 0
    execution_reject_rate: float = 0.0
    max_execution_reject_rate: float = 0.01
    require_paper_trade_governance: bool = False
    paper_trade_summary_path: str | None = None
    min_paper_trade_calendar_days: int = 7
    min_paper_trade_trading_days: int = 5
    min_paper_trade_session_minutes: int = 60
    min_sharpe_oos: float = 1.0
    max_abs_drawdown: float = 0.2
    max_turnover: float = 2.0
    max_correlation: float = 0.7
    # Slice C: minimum replay-parity match percentage required for Gate D promotion.
    # Scorecards under a strict validation profile must populate
    # ``scorecard.replay_parity.match_pct``; missing or below-threshold values
    # fail closed (see _evaluate_gate_d).
    min_replay_parity_match_pct: float = 95.0
    # Slice B Task 12: belt-and-suspenders Gate D audits that mirror the
    # ``inventory_mtm`` and ``cost_uncertainty`` Gate C sub-gates. The audits
    # read sub-gate metrics that Gate C wrote to the scorecard and re-check
    # them at promotion time. Defaults preserve current behaviour for
    # scorecards that do not carry the Slice B sub-gate payloads.
    min_inventory_mtm_safety_margin_pct: float = 5.0
    min_cost_uncertainty_p95_lower_bound_pts: float = 0.0
    enable_rust_readiness_gate: bool = False
    rust_module_name: str | None = None
    rust_parity_test_path: str = "tests/unit/test_rust_hotpath_parity.py"
    rust_parity_timeout_s: int = 180
    enforce_rust_benchmark_gate: bool = False
    rust_benchmark_cmd: str = (
        "uv run python tests/benchmark/perf_regression_gate.py "
        "--baseline tests/benchmark/.benchmark_baseline.json "
        "--current benchmark.json "
        "--threshold 0.10"
    )
    canary_weight: float | None = None
    expiry_days: int = 30
    max_live_slippage_bps: float = 3.0
    max_live_drawdown_contribution: float = 0.02
    max_execution_error_rate: float = 0.01
    force: bool = False
    force_reason: str = ""
    write_promotion_config: bool = True
    config_version: str = "v1"
    parent_config_version: str | None = None
    # Feature set version from the alpha manifest.  When set, Gate D warns
    # (warn-only, non-blocking) if it doesn't match the live engine version.
    manifest_feature_set_version: str | None = None
    # Strict validation profile required for Gate D entry.
    validation_profile: Any | None = None
    # L6: equity provenance guard. When True (default) Gate D fails closed if
    # the scorecard reports ``equity_source="synthetic"`` or omits the field.
    # ``real`` and ``real_no_trade`` are accepted (a confirmed no-fill
    # session is a legitimate observation, not a synthetic fallback).
    require_real_equity: bool = True
    # L6: artifact provenance guard. When True (default) promotion refuses
    # scorecards stamped ``screen_only=true`` (produced by ``hft alpha screen``).
    reject_screen_only: bool = True
    # Loop_v1 L11 stabilization: ``--dry-run`` runs Gate D evaluation without
    # any durable side effects -- no kill-ledger row, no
    # ``config/strategy_promotions/`` YAML write. Research artifacts (decision
    # JSON, integration report) still land under the experiments dir for
    # auditability. Set via ``hft alpha promote --dry-run``.
    dry_run: bool = False
    # ``governed`` promotes only a sealed scorecard under the evidence root,
    # writes its reports there, appends Gate D/E/F events to the verdict
    # ledger and never rewrites ``impl.py`` (ADR-005 section 6).
    run_mode: str = "exploratory"


@dataclass(frozen=True)
class PromotionChecklistItem:
    label: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class PromotionChecklist:
    items: list[PromotionChecklistItem]

    def all_passed(self) -> bool:
        return all(i.passed for i in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "all_passed": self.all_passed(),
            "items": [{"label": i.label, "passed": i.passed, "detail": i.detail} for i in self.items],
        }


@dataclass(frozen=True)
class PromotionResult:
    alpha_id: str
    approved: bool
    forced: bool
    gate_d_passed: bool
    gate_e_passed: bool
    gate_f_passed: bool
    canary_weight: float
    integration_report_path: str
    promotion_decision_path: str
    promotion_config_path: str | None
    reasons: list[str]
    paper_governance_report_path: str | None = None
    checklist: PromotionChecklist | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.checklist is not None:
            d["checklist"] = self.checklist.to_dict()
        return d


def promote_alpha(config: PromotionConfig) -> PromotionResult:  # noqa: C901
    profile = getattr(config, "validation_profile", None)
    if profile is None or not getattr(profile, "is_strict", False):
        raise PromotionError(
            f"strict profile required for Gate D entry; got profile={getattr(profile, 'name', None)!r}"
        )
    root = Path(config.project_root).resolve()
    alpha_dir = root / "research" / "alphas" / config.alpha_id
    alpha_dir.mkdir(parents=True, exist_ok=True)

    scorecard_path = _resolve_scorecard_path(root, config, alpha_dir)
    if not scorecard_path.exists():
        raise FileNotFoundError(f"scorecard not found: {scorecard_path}")

    writer, commit = _governed_promotion_writer(config, root, scorecard_path)
    _verify_sealed_scorecard(scorecard_path)
    scorecard = json.loads(scorecard_path.read_text())

    # L6 (#341): refuse screen-only artifacts before any further gate evaluation.
    if config.reject_screen_only and bool(scorecard.get("screen_only", False)):
        raise PromotionError(
            f"cannot_promote_screen_artifact: scorecard {scorecard_path} is stamped "
            "screen_only=true; run `hft alpha validate --profile strict` to produce "
            "a promotion-eligible artifact"
        )

    # Verify Gate C passed before evaluating promotion gates.
    # Slice-D T14: on raise, log a kill-ledger row (gate='C') before re-raising
    # so the rejected alpha is recorded in the durable ledger. The original
    # ``ValueError`` is preserved for backward compatibility with existing
    # callers that match on its message.
    # A kill is recorded only when the stored Gate C verdict is KILL; an
    # INVALID / NEEDS-MORE-DAYS / INCONCLUSIVE run is refused without one.
    try:
        _verify_gate_c_passed(scorecard_path)
    except GateCNotPassedError as exc:
        if not config.dry_run and exc.verdict is not None and exc.verdict.verdict is Verdict.KILL:
            _auto_kill(
                alpha_id=config.alpha_id,
                gate="C",
                verdict=exc.verdict,
                run_id=exc.run_id,
                alpha_dir=alpha_dir,
                scorecard_path=scorecard_path,
            )
        raise

    data_ul_value = _to_float(scorecard.get("data_ul"))
    data_ul = int(data_ul_value) if data_ul_value is not None else None
    data_ul_advisory = {
        "value": data_ul,
        "gate_d_recommended_min": 4,
        "gate_d_warn": (data_ul is None or data_ul < 4),
        "gate_e_recommended_min": 5,
        "gate_e_warn": (data_ul is None or data_ul < 5),
        "blocking": False,
    }
    gate_d_verdict, gate_d_checks = _evaluate_gate_d_verdict(scorecard, config)
    gate_d_passed = gate_d_verdict.passed
    # Status is derived from the verdict ledger; promotion never rewrites impl.py.
    if not gate_d_passed and not config.dry_run and gate_d_verdict.verdict is Verdict.KILL:
        # Slice-D T14: log a kill-ledger row (gate='D') for a KILL verdict.
        # Gate D failure does not raise here -- promote_alpha returns a
        # PromotionResult with approved=False -- so this is the *only* path
        # where the kill is recorded for the Gate-D outcome. A Gate D check
        # that could not be evaluated (missing scorecard field) is INVALID,
        # not a kill. Dry-run mode (Loop_v1 L11) suppresses the write.
        _auto_kill(
            alpha_id=config.alpha_id,
            gate="D",
            verdict=gate_d_verdict,
            run_id=_scorecard_run_id(scorecard_path),
            alpha_dir=alpha_dir,
            scorecard_path=scorecard_path,
        )

    gate_e_passed, gate_e_checks = _evaluate_gate_e(config, root)

    gate_f_passed, gate_f_checks = _evaluate_gate_f(config, root)

    approved = gate_d_passed and gate_e_passed and gate_f_passed
    forced = False
    reasons: list[str] = []
    if not approved:
        reasons.append("One or more promotion gates failed")
        if config.force:
            if not config.force_reason.strip():
                raise ValueError("force=True requires a non-empty force_reason")
            approved = True
            forced = True
            _log.warning(
                "promotion_force_override",
                alpha_id=config.alpha_id,
                force_reason=config.force_reason,
            )
            reasons.append(f"force=true override: {config.force_reason}")

    weight = _suggest_canary_weight(scorecard, override=config.canary_weight) if approved else 0.0
    now = datetime.fromtimestamp(timebase.now_s(), tz=_dt.timezone.utc)
    expiry = (now + timedelta(days=max(config.expiry_days, 1))).date().isoformat()
    artifact_dir = _promotion_artifact_dir(
        writer.root if writer else root / "research" / "experiments", config.alpha_id, now
    )

    integration_report = {
        "alpha_id": config.alpha_id,
        "gate": "Gate D",
        "passed": gate_d_passed,
        "verdict": gate_d_verdict.to_dict(),
        "checks": gate_d_checks,
        "data_ul_advisory": data_ul_advisory,
        "scorecard_path": str(scorecard_path),
        "timestamp": now.isoformat(),
    }
    integration_report_path = artifact_dir / "integration_report.json"
    _write_json(integration_report_path, integration_report)

    paper_governance_report = _build_paper_governance_report(config, gate_e_checks)
    paper_governance_report_path = artifact_dir / "paper_governance_report.json"
    _write_json(paper_governance_report_path, paper_governance_report)

    obligations = _promotion_obligations(gate_d_verdict, config)
    decision_payload = {
        "alpha_id": config.alpha_id,
        "gate_d_passed": gate_d_passed,
        "gate_d_verdict": gate_d_verdict.verdict.value,
        # Deferred gates this approval still owes to a later stage (ADR-003).
        "obligations": obligations,
        "gate_e_passed": gate_e_passed,
        "gate_f_passed": gate_f_passed,
        "gate_e_checks": gate_e_checks,
        "gate_f_checks": gate_f_checks,
        "data_ul_advisory": data_ul_advisory,
        "decision": "approve" if approved else "reject",
        "forced": forced,
        "reasons": reasons,
        "canary_weight": weight,
        "paper_governance_report_path": str(paper_governance_report_path),
        "paper_governance_passed": bool(paper_governance_report.get("passed", False)),
        "timestamp": now.isoformat(),
    }
    decision_path = artifact_dir / "promotion_decision.json"
    _write_json(decision_path, decision_payload)
    if writer is not None:
        _publish_governed_promotion(
            writer,
            config=config,
            commit=commit,
            artifact_dir=artifact_dir,
            scorecard_path=scorecard_path,
            gates={
                "gate_d": (gate_d_passed, gate_d_verdict.verdict.value),
                "gate_e": (gate_e_passed, None),
                "gate_f": (gate_f_passed, None),
            },
        )

    promotion_config_path: Path | None = None
    if approved and config.write_promotion_config and not config.dry_run:
        promotion_config_path = _write_promotion_config(
            root=root,
            config=config,
            canary_weight=weight,
            expiry_review_date=expiry,
            scorecard=scorecard,
            approved=approved,
            forced=forced,
            obligations=obligations,
        )
    elif approved and config.dry_run:
        reasons.append("dry_run=true: promotion config not persisted")
    elif approved and not config.write_promotion_config:
        reasons.append("promotion_config_write_disabled=true (research-only run)")

    checklist = build_promotion_checklist(config, gate_d_checks, gate_e_checks, gate_f_checks)

    result = PromotionResult(
        alpha_id=config.alpha_id,
        approved=approved,
        forced=forced,
        gate_d_passed=gate_d_passed,
        gate_e_passed=gate_e_passed,
        gate_f_passed=gate_f_passed,
        canary_weight=weight,
        integration_report_path=str(integration_report_path),
        promotion_decision_path=str(decision_path),
        paper_governance_report_path=str(paper_governance_report_path),
        promotion_config_path=str(promotion_config_path) if promotion_config_path else None,
        reasons=reasons,
        checklist=checklist,
    )

    # Best-effort audit logging (guarded by HFT_ALPHA_AUDIT_ENABLED)
    try:
        from hft_platform.alpha.audit import log_gate_result, log_promotion_result
        from hft_platform.alpha.validation import GateReport

        gate_d_report = GateReport(gate="Gate D", passed=gate_d_passed, details=gate_d_checks)
        gate_e_report = GateReport(gate="Gate E", passed=gate_e_passed, details=gate_e_checks)
        gate_f_report = GateReport(gate="Gate F", passed=gate_f_passed, details=gate_f_checks)
        cfg_hash = scorecard.get("config_hash", "")
        for gate_report in (gate_d_report, gate_e_report, gate_f_report):
            log_gate_result(config.alpha_id, None, gate_report, cfg_hash)
        log_promotion_result(result, cfg_hash, scorecard)
    except Exception as _exc:  # noqa: BLE001
        _log.debug("audit_log_failed", alpha_id=config.alpha_id, exc_info=True)

    return result


# Gate D checks whose input is a scorecard value: a missing value means the
# check could not be evaluated (ERROR), not that the candidate failed it.
_GATE_D_VALUE_REQUIRED: frozenset[str] = frozenset(
    {"sharpe_oos", "max_drawdown", "turnover", "correlation_pool_max", "latency_profile", "replay_parity_audit"}
)
# Gate D checks that audit the evidence (latency assumptions, equity
# provenance, replay fidelity, feature-set version) rather than the
# candidate. Failing one makes the evidence unusable (ERROR): it does not
# show the candidate is dead.
_GATE_D_EVIDENCE_CHECKS: frozenset[str] = frozenset(
    {"latency_profile", "latency_audit_strict", "equity_source", "replay_parity_audit", "feature_set_version"}
)


def _evaluate_gate_d(scorecard: dict[str, Any], config: PromotionConfig) -> tuple[bool, dict[str, Any]]:
    verdict, checks = _evaluate_gate_d_verdict(scorecard, config)
    return verdict.passed, checks


def _evaluate_gate_d_verdict(
    scorecard: dict[str, Any], config: PromotionConfig
) -> tuple[VerdictResult, dict[str, Any]]:
    """Gate D checks, each with an ``outcome``, and the verdict over them.

    Gate D runs only after Gate C recorded a PASS, which includes the sample
    floor, so a Gate D FAIL counts as powered.
    """
    checks = _gate_d_checks(scorecard, config)
    for name, check in checks.items():
        if "outcome" in check:
            # An outcome set by the check must agree with its pass flag;
            # a contradiction is an ERROR, never whichever side says PASS.
            try:
                resolve_outcome(bool(check["pass"]), check["outcome"])
            except ValueError:
                check["outcome"] = Outcome.ERROR.value
        else:
            missing = name in _GATE_D_VALUE_REQUIRED and check.get("value") is None
            if missing or (name in _GATE_D_EVIDENCE_CHECKS and not check["pass"]):
                check["outcome"] = Outcome.ERROR.value
            else:
                check["outcome"] = (Outcome.PASS if check["pass"] else Outcome.FAIL).value
    # Gate D audits re-check Gate C sub-gates under ``<sub_gate>_audit``; a
    # sub-gate the profile defers is deferred here under the audit's name.
    profile = getattr(config, "validation_profile", None)
    deferrable = {f"{gate}_audit": stage for gate, stage in (getattr(profile, "deferrable", None) or {}).items()}
    verdict = derive_verdict(
        [{"name": name, "outcome": check["outcome"]} for name, check in checks.items()],
        blocking={name for name, check in checks.items() if check.get("blocking", True)},
        deferrable=deferrable,
        powered=True,
    )
    return verdict, checks


def _replay_parity_audit(scorecard: dict[str, Any], config: PromotionConfig) -> dict[str, Any]:
    """Gate D ``replay_parity_audit``: absent defers (ADR-003); present but unusable is an ERROR."""
    replay_parity = scorecard.get("replay_parity")
    match_pct: float | None = None
    if isinstance(replay_parity, dict):
        match_pct = _to_float(replay_parity.get("match_pct"))
        if match_pct is not None and not math.isfinite(match_pct):
            match_pct = None
    min_match_pct = getattr(config, "min_replay_parity_match_pct", 95.0)
    if replay_parity is not None and match_pct is None:
        # A report exists but carries no usable match_pct (malformed, or an
        # error record): that is broken evidence, not absent evidence, so it
        # must not be deferred as NOT_APPLICABLE.
        return {
            "value": None,
            "min": min_match_pct,
            "required": True,
            "outcome": Outcome.ERROR.value,
            "pass": False,
            "detail": "INVALID — scorecard.replay_parity present but has no numeric match_pct",
        }
    if match_pct is None:
        # No parity report: a research candidate has no live intents yet
        # (ADR-003). NOT_APPLICABLE is deferred to Shadow exit only when the
        # profile says so; otherwise the run is INVALID.
        return {
            "value": None,
            "min": min_match_pct,
            "required": True,
            "outcome": Outcome.NOT_APPLICABLE.value,
            "pass": False,
            "detail": "MISSING — scorecard.replay_parity absent; no live intents to compare (ADR-003)",
        }
    return {
        "value": match_pct,
        "min": min_match_pct,
        "required": True,
        "pass": match_pct >= min_match_pct,
        "detail": "OK" if match_pct >= min_match_pct else f"match_pct {match_pct} below {min_match_pct}",
    }


def _gate_d_checks(scorecard: dict[str, Any], config: PromotionConfig) -> dict[str, Any]:
    from hft_platform.alpha.latency_audit import latency_audit as _latency_audit

    sharpe = _to_float(scorecard.get("sharpe_oos"))
    max_dd = _to_float(scorecard.get("max_drawdown"))
    turnover = _to_float(scorecard.get("turnover"))
    corr = _to_float(scorecard.get("correlation_pool_max"))
    latency_profile = scorecard.get("latency_profile") or None
    # Slice B Task 12 — strict-mode P95 fail-closed check. Only triggered when
    # the scorecard carries a *resolved* profile dict (i.e. expanded from
    # config/research/latency_profiles.yaml). String-only metadata preserves
    # the existing latency_profile-presence check below.
    profile_obj = getattr(config, "validation_profile", None)
    profile_is_strict = bool(getattr(profile_obj, "is_strict", False))
    if isinstance(latency_profile, dict):
        _audit = _latency_audit(latency_profile, strict=profile_is_strict)
    else:
        _audit = None

    checks: dict[str, dict[str, Any]] = {
        "sharpe_oos": {
            "value": sharpe,
            "min": config.min_sharpe_oos,
            "pass": (sharpe is not None and sharpe >= config.min_sharpe_oos),
        },
        "max_drawdown": {
            "value": max_dd,
            "min": -abs(config.max_abs_drawdown),
            "pass": (max_dd is not None and max_dd >= -abs(config.max_abs_drawdown)),
        },
        "turnover": {
            "value": turnover,
            "max": config.max_turnover,
            "pass": (turnover is not None and turnover <= config.max_turnover),
        },
        "correlation_pool_max": {
            "value": corr,
            "max": config.max_correlation,
            "required": True,
            "pass": (corr is not None and corr <= config.max_correlation),
            "detail": (
                "OK"
                if corr is not None
                else "MISSING — scorecard.correlation_pool_max must be populated before promotion"
            ),
        },
        # Latency realism governance (CLAUDE.md constitution requirement).
        # Missing latency_profile in the scorecard = NOT promotion-ready.
        # Blocks Gate D: alpha must record P95 broker RTT assumptions before promotion.
        "latency_profile": {
            "value": latency_profile,
            "required": True,
            "pass": latency_profile is not None,
            "detail": (
                "OK"
                if latency_profile
                else "MISSING — must record P95 Shioaji broker RTT assumptions "
                "(see docs/architecture/latency-baseline-shioaji-sim-vs-system.md)"
            ),
        },
    }
    # L6: equity provenance audit — ``synthetic`` (HTML-report fallback) is
    # fail-closed; ``real`` and ``real_no_trade`` (confirmed no-fill session,
    # length-valid + zero P&L motion + zero fills) are accepted; missing is
    # warn-only for back-compat with legacy scorecards (the field was
    # introduced in loop_v1 L6 and older runs lack it).
    if config.require_real_equity:
        allowed_sources = ("real", "real_no_trade")
        equity_source_raw = scorecard.get("equity_source")
        equity_source = str(equity_source_raw).strip() if isinstance(equity_source_raw, str) else None
        if equity_source == "synthetic":
            equity_pass = False
            equity_detail = (
                "REJECTED — scorecard.equity_source='synthetic'; re-run the "
                "backtest under strict_equity=True with a real equity series"
            )
        elif equity_source in allowed_sources:
            equity_pass = True
            equity_detail = "OK"
        elif equity_source is None:
            # Legacy scorecard predating L6 — pass with warning.
            equity_pass = True
            equity_detail = (
                "WARN — scorecard.equity_source missing (legacy); future runs "
                "should populate {real, real_no_trade} per loop_v1 L6"
            )
        else:
            equity_pass = False
            equity_detail = f"REJECTED — equity_source={equity_source!r} not in {list(allowed_sources)}"
        checks["equity_source"] = {
            "value": equity_source,
            "allowed": list(allowed_sources),
            "required": True,
            "pass": equity_pass,
            "detail": equity_detail,
        }
    # Slice B Task 12 — surface the strict P95 fail-closed audit when a
    # resolved profile dict is attached. String-only metadata leaves only
    # the legacy latency_profile-presence check above (advisory until a
    # resolved dict is provided upstream).
    if _audit is not None:
        checks["latency_audit_strict"] = {
            "value": _audit.get("profile_id"),
            "required": profile_is_strict,
            "pass": bool(_audit.get("passed", False)),
            "detail": str(_audit.get("reason", "")),
        }
    # Slice C: replay-parity audit — mirrors latency_profile fail-closed semantics.
    # Promotion-time check that the scorecard recorded a passing replay_parity_report
    # (live → backtest intent reconstruction within tolerance under a strict profile).
    checks["replay_parity_audit"] = _replay_parity_audit(scorecard, config)

    # Slice B Task 12: inventory_mtm + cost_uncertainty audits.
    # Belt-and-suspenders re-check of the Gate C sub-gate results that the
    # strict profile elevates to blocking. The audits read the sub-gate
    # payloads Gate C wrote to the scorecard (passed/metrics) — they do NOT
    # re-evaluate from raw daily_pnl. Pattern mirrors replay_parity_audit above.
    strategy_type = str(scorecard.get("strategy_type") or "").strip().lower()

    # --- inventory_mtm_audit (maker-only; advisory PASS for taker) ---
    inventory_mtm_payload = scorecard.get("inventory_mtm")
    if strategy_type == "taker":
        checks["inventory_mtm_audit"] = {
            "value": None,
            "required": False,
            "blocking": False,
            "outcome": Outcome.NOT_APPLICABLE.value,
            "pass": True,
            "detail": "not applicable to taker strategies (inventory_mtm is a maker audit)",
        }
    elif isinstance(inventory_mtm_payload, dict):
        _inv_raw = inventory_mtm_payload.get("metrics")
        inv_metrics: dict[str, Any] = _inv_raw if isinstance(_inv_raw, dict) else {}
        sub_passed = bool(inventory_mtm_payload.get("passed", False))
        inv_outcome = _payload_outcome(inventory_mtm_payload)
        net_pts = _to_float(inv_metrics.get("net_pts"))
        cost_floor_total = _to_float(inv_metrics.get("cost_floor_total_pts"))
        checks["inventory_mtm_audit"] = {
            "value": net_pts,
            "cost_floor_total_pts": cost_floor_total,
            "safety_margin_pct": float(config.min_inventory_mtm_safety_margin_pct),
            "required": True,
            "outcome": inv_outcome,
            "pass": sub_passed,
            "detail": (
                "OK" if sub_passed else str(inventory_mtm_payload.get("details") or "inventory_mtm sub-gate FAILED")
            ),
        }
    else:
        # Maker / unknown strategy with no inventory_mtm payload: the audit
        # cannot be evaluated, which is an ERROR, not a default PASS.
        checks["inventory_mtm_audit"] = {
            "value": None,
            "required": True,
            "outcome": Outcome.ERROR.value,
            "pass": False,
            "detail": "MISSING — scorecard.inventory_mtm absent; the audit cannot be evaluated",
        }

    # --- cost_uncertainty_audit (applies to maker + taker) ---
    cost_uncertainty_payload = scorecard.get("cost_uncertainty")
    min_p95_lower = float(config.min_cost_uncertainty_p95_lower_bound_pts)
    if isinstance(cost_uncertainty_payload, dict):
        _cu_raw = cost_uncertainty_payload.get("metrics")
        cu_metrics: dict[str, Any] = _cu_raw if isinstance(_cu_raw, dict) else {}
        sub_passed = bool(cost_uncertainty_payload.get("passed", False))
        p95_lower = _to_float(cu_metrics.get("p95_lower_bound_pts"))
        checks["cost_uncertainty_audit"] = {
            "value": p95_lower,
            "min": min_p95_lower,
            "required": True,
            "outcome": _payload_outcome(cost_uncertainty_payload),
            "pass": sub_passed,
            "detail": (
                "OK"
                if sub_passed
                else str(cost_uncertainty_payload.get("details") or "cost_uncertainty sub-gate FAILED")
            ),
        }
    else:
        # No cost_uncertainty payload: the audit cannot be evaluated (ERROR).
        checks["cost_uncertainty_audit"] = {
            "value": None,
            "min": min_p95_lower,
            "required": True,
            "outcome": Outcome.ERROR.value,
            "pass": False,
            "detail": "MISSING — scorecard.cost_uncertainty absent; the audit cannot be evaluated",
        }
    # Feature set version parity check (warn-only: does NOT block Gate D).
    manifest_fsv = str(config.manifest_feature_set_version or "").strip() or None
    _LIVE_FSV: str | None = None
    try:
        from hft_platform.feature.registry import FEATURE_SET_VERSION as _LIVE_FSV
    except Exception as _exc:  # noqa: BLE001
        pass
    if manifest_fsv is not None and _LIVE_FSV is not None:
        fsv_match = manifest_fsv == _LIVE_FSV
        checks["feature_set_version"] = {
            "manifest": manifest_fsv,
            "live_engine": _LIVE_FSV,
            "match": fsv_match,
            "pass": fsv_match,  # blocking: mismatch fails Gate D
            "detail": (
                "OK"
                if fsv_match
                else f"MISMATCH — manifest declares '{manifest_fsv}' but live engine uses '{_LIVE_FSV}'. "
                "Re-run backtest with the current feature set before promoting to live."
            ),
        }

    return checks


def _resolve_scorecard_path(root: Path, config: PromotionConfig, alpha_dir: Path) -> Path:
    if config.scorecard_path:
        path = Path(config.scorecard_path)
        if not path.is_absolute():
            path = root / path
        return path

    latest = _latest_scorecard_from_runs(root, config.alpha_id, config.experiments_dir)
    if latest is not None:
        return latest

    # Backward-compatible fallback for legacy/manual runs.
    return alpha_dir / "scorecard.json"


def _verify_gate_c_passed(scorecard_path: Path) -> None:
    """Verify Gate C has passed before allowing promotion to proceed.

    Reads meta.json from the same directory as the scorecard.  If the file
    exists and indicates gate_c=False (or the field is missing), raises
    ValueError to prevent promoting an alpha that has not been properly
    validated.  If meta.json is absent (legacy / manual scorecard) a WARNING
    is emitted and execution continues for backward compatibility.
    """
    meta_path = scorecard_path.parent / "meta.json"
    if not meta_path.exists():
        _log.warning(
            "gate_c_verification_skipped_no_meta",
            scorecard_path=str(scorecard_path),
            reason="meta.json not found; assuming legacy/manual scorecard — proceeding without Gate C verification",
        )
        return

    meta = json.loads(meta_path.read_text())
    run_id = str(meta.get("run_id") or "")
    verdict = _stored_gate_c_verdict(scorecard_path.parent / "backtest_report.json")
    message = f"Gate C has not passed for this scorecard: {scorecard_path}. Run validate_alpha() first."
    gate_status = meta.get("gate_status")
    if gate_status is None or not gate_status.get("gate_c", False):
        if verdict is not None:
            message = f"{message} (verdict={verdict.verdict.value})"
        raise GateCNotPassedError(message, verdict=verdict, run_id=run_id)
    if verdict is not None and not verdict.passed:
        raise GateCNotPassedError(
            f"{message} (gate_status says passed but the stored verdict is {verdict.verdict.value})",
            verdict=verdict,
            run_id=run_id,
        )


def record_gate_c_kill(*, alpha_id: str, project_root: str | Path, scorecard_path: str | Path) -> bool:
    """Write the kill for a run whose stored Gate C verdict is KILL.

    Callers that skip promotion on a non-PASS validation use this so a real
    KILL still reaches the ledger; any other verdict (or none) writes nothing.
    The kill id carries the run id, so a repeated call is deduplicated by the
    ledger. Returns True when the stored verdict is KILL.
    """
    sc_path = Path(scorecard_path)
    verdict = _stored_gate_c_verdict(sc_path.parent / "backtest_report.json")
    if verdict is None or verdict.verdict is not Verdict.KILL:
        return False
    _auto_kill(
        alpha_id=alpha_id,
        gate="C",
        verdict=verdict,
        run_id=_scorecard_run_id(sc_path),
        alpha_dir=Path(project_root) / "research" / "alphas" / alpha_id,
        scorecard_path=sc_path,
    )
    return True


def _stored_gate_c_verdict(backtest_report_path: Path) -> VerdictResult | None:
    """The verdict Gate C wrote into the run's backtest report, if any."""
    if not backtest_report_path.exists():
        return None
    try:
        payload = json.loads(backtest_report_path.read_text())
        raw = (payload.get("details") or {}).get("verdict")
        return VerdictResult.from_dict(raw) if isinstance(raw, dict) else None
    except (OSError, ValueError, AttributeError, TypeError):
        _log.warning("gate_c_verdict_unreadable", path=str(backtest_report_path), exc_info=True)
        return None


def _payload_outcome(payload: dict[str, Any]) -> str:
    """Outcome a stored sub-gate payload carries, checked against its ``passed``.

    A payload whose ``passed`` and ``outcome`` contradict each other, whose
    outcome is unknown, or that carries neither, cannot be trusted and is an
    ERROR: a failed audit must never reach promotion as a PASS.
    """
    passed = payload.get("passed")
    try:
        return resolve_outcome(None if passed is None else bool(passed), payload.get("outcome") or None).value
    except ValueError:
        return Outcome.ERROR.value


def _latest_scorecard_from_runs(
    root: Path, alpha_id: str, experiments_dir: str = "research/experiments"
) -> Path | None:
    try:
        from hft_platform.alpha.experiments import ExperimentTracker

        tracker = ExperimentTracker(base_dir=root / experiments_dir)
        for row in tracker.list_runs(alpha_id=alpha_id):
            if not row.scorecard_path:
                continue
            scorecard = Path(row.scorecard_path)
            if scorecard.exists():
                return scorecard
    except Exception as _exc:  # noqa: BLE001
        return None
    return None


def _governed_promotion_writer(
    config: PromotionConfig, root: Path, scorecard_path: Path
) -> tuple[EvidenceWriter | None, str | None]:
    """The evidence writer and commit for a governed promotion; ``(None, None)`` when exploratory.

    A governed promotion needs the governed-run preconditions, the evidence
    root as ``experiments_dir``, and a sealed scorecard under that root.
    """
    if RunMode(config.run_mode) is not RunMode.GOVERNED:
        return None, None
    commit, dirty = git_state(root)
    profile_name = getattr(config.validation_profile, "name", None) or None
    evidence = require_governed(git_commit=commit, git_dirty=dirty, profile_name=profile_name)
    reasons = []
    if (root / config.experiments_dir).resolve() != evidence:
        reasons.append(f"experiments_dir {config.experiments_dir} is not the evidence root {evidence}")
    writer = EvidenceWriter(evidence)
    try:
        writer.relpath(scorecard_path)
    except ValueError:
        reasons.append(f"scorecard {scorecard_path} is not under the evidence root {evidence}")
    if not is_sealed(scorecard_path.parent):
        reasons.append(f"scorecard {scorecard_path} is not sealed evidence")
    if reasons:
        raise GovernedRunRefused(reasons)
    return writer, commit


def _verify_sealed_scorecard(scorecard_path: Path) -> None:
    """Refuse a sealed scorecard whose directory no longer matches its manifest."""
    if not is_sealed(scorecard_path.parent):
        return
    changed = EvidenceWriter.verify(scorecard_path.parent)
    if changed:
        raise PromotionError(f"sealed evidence {scorecard_path.parent} changed since it was sealed: {changed}")


def _publish_governed_promotion(
    writer: EvidenceWriter,
    *,
    config: PromotionConfig,
    commit: str | None,
    artifact_dir: Path,
    scorecard_path: Path,
    gates: dict[str, tuple[bool, str | None]],
) -> None:
    """Seal the promotion reports and append one ledger event per promotion gate."""
    manifest = writer.seal(artifact_dir, alpha_id=config.alpha_id, mode=RunMode.GOVERNED)
    scorecard_manifest = scorecard_path.parent / "manifest.json"
    evidence = {
        writer.relpath(manifest): file_sha256(manifest, cached=False),
        writer.relpath(scorecard_manifest): file_sha256(scorecard_manifest, cached=False),
    }
    for stage, (passed, verdict) in gates.items():
        event: dict[str, Any] = {
            "alpha_id": config.alpha_id,
            "run_id": _scorecard_run_id(scorecard_path) or None,
            "git_commit": commit,
            "profile": getattr(config.validation_profile, "name", None),
            "evidence": evidence,
            "stage": stage,
            "passed": bool(passed),
        }
        if verdict is not None:
            event["verdict"] = verdict
        writer.append_verdict(event, mode=RunMode.GOVERNED)


def _promotion_artifact_dir(base: Path, alpha_id: str, now: datetime) -> Path:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    out = base / "promotions" / alpha_id / f"{stamp}_{uuid4().hex[:8]}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _evaluate_gate_e(config: PromotionConfig, root: Path) -> tuple[bool, dict[str, Any]]:
    paper_summary, summary_source, summary_error = _resolve_paper_trade_summary(config, root)
    summary_session_count = int(paper_summary.get("session_count", 0)) if paper_summary else 0
    summary_calendar_days = int(paper_summary.get("calendar_span_days", 0)) if paper_summary else 0
    summary_trading_days = int(paper_summary.get("distinct_trading_days", 0)) if paper_summary else 0
    summary_min_session_seconds = int(paper_summary.get("min_session_duration_seconds", 0)) if paper_summary else 0
    summary_invalid_session_durations = (
        int(paper_summary.get("invalid_session_duration_count", summary_session_count)) if paper_summary else 0
    )
    summary_drift_alerts = int(paper_summary.get("drift_alerts_total", 0)) if paper_summary else 0
    summary_reject_rate_mean = float(paper_summary.get("execution_reject_rate_mean", 0.0)) if paper_summary else 0.0
    # Prefer P95 reject rate over mean per CLAUDE.md latency realism policy.
    # Falls back to mean if P95 not recorded (legacy sessions).
    _p95_raw = paper_summary.get("execution_reject_rate_p95") if paper_summary else None
    summary_reject_rate = float(_p95_raw) if _p95_raw is not None else summary_reject_rate_mean

    shadow_sessions = summary_session_count if paper_summary else int(config.shadow_sessions)
    drift_alerts = summary_drift_alerts if paper_summary else int(config.drift_alerts)
    execution_reject_rate = summary_reject_rate if paper_summary else float(config.execution_reject_rate)

    checks: dict[str, dict[str, Any]] = {
        "shadow_sessions": {
            "value": shadow_sessions,
            "min": config.min_shadow_sessions,
            "pass": shadow_sessions >= config.min_shadow_sessions,
        },
        "drift_alerts": {"value": drift_alerts, "max": 0, "pass": drift_alerts == 0},
        "execution_reject_rate": {
            "value": execution_reject_rate,
            "max": config.max_execution_reject_rate,
            "pass": execution_reject_rate <= config.max_execution_reject_rate,
            "source": "p95" if _p95_raw is not None else "mean",
        },
    }

    mode = "manual_shadow"
    if config.require_paper_trade_governance:
        mode = "paper_trade_governed"
        has_summary = bool(paper_summary) and summary_error is None
        checks["paper_trade_log_available"] = {
            "value": has_summary,
            "required": True,
            "pass": has_summary,
            "source": summary_source,
            "error": summary_error,
        }
        checks["paper_trade_calendar_days"] = {
            "value": summary_calendar_days if has_summary else 0,
            "min": int(config.min_paper_trade_calendar_days),
            "pass": has_summary and summary_calendar_days >= int(config.min_paper_trade_calendar_days),
        }
        checks["paper_trade_trading_days"] = {
            "value": summary_trading_days if has_summary else 0,
            "min": int(config.min_paper_trade_trading_days),
            "pass": has_summary and summary_trading_days >= int(config.min_paper_trade_trading_days),
        }
        required_session_seconds = max(60, int(config.min_paper_trade_session_minutes) * 60)
        checks["paper_trade_session_duration"] = {
            "value": summary_min_session_seconds if has_summary else 0,
            "min_seconds": int(required_session_seconds),
            "min_minutes": int(config.min_paper_trade_session_minutes),
            "invalid_session_duration_count": (
                summary_invalid_session_durations if has_summary else int(shadow_sessions)
            ),
            "pass": (
                has_summary
                and summary_invalid_session_durations == 0
                and summary_min_session_seconds >= int(required_session_seconds)
            ),
        }

    # Regime-span check (warn-only: does NOT block Gate E).
    # Sessions should cover ≥2 distinct regimes for adequate test diversity.
    regimes_covered = list(paper_summary.get("regimes_covered", [])) if paper_summary else []
    regime_span_check: dict[str, object] = {"covered": regimes_covered, "pass": True}
    if len(regimes_covered) < 2:
        regime_span_check["warning"] = (
            "Paper-trade sessions do not span ≥2 regimes "
            "(recommended: trending + mean_reverting). Increase test diversity."
        )
    checks["regime_span"] = regime_span_check

    passed = all(bool(v.get("pass", True)) for v in checks.values())
    return passed, {
        "mode": mode,
        "checks": checks,
        "paper_trade_summary_source": summary_source,
        "paper_trade_summary_error": summary_error,
        "paper_trade_summary": paper_summary,
    }


def _evaluate_gate_f(config: PromotionConfig, root: Path) -> tuple[bool, dict[str, Any]]:
    if not bool(config.enable_rust_readiness_gate):
        return True, {"skipped": True, "reason": "enable_rust_readiness_gate=false", "checks": {}}
    profile = getattr(config, "validation_profile", None)
    na_reason = (getattr(profile, "not_applicable_gates", None) or {}).get("gate_f")
    if na_reason:
        # The profile declares Gate F not applicable (Rust is paused): it
        # does not block, and it is recorded as NOT_APPLICABLE, not as a PASS.
        return True, {
            "skipped": True,
            "outcome": Outcome.NOT_APPLICABLE.value,
            "reason": f"not_applicable:{na_reason}",
            "profile": getattr(profile, "name", ""),
            "checks": {},
        }

    rust_module = str(config.rust_module_name or "").strip() or _load_rust_module_name(root, config.alpha_id)
    checks: dict[str, dict[str, Any]] = {
        "rust_module_declared": {
            "value": rust_module or None,
            "required": True,
            "pass": bool(rust_module),
        }
    }
    if not rust_module:
        checks["rust_parity_tests"] = {
            "pass": False,
            "detail": "Skipped because rust_module is not declared in manifest",
        }
        if config.enforce_rust_benchmark_gate:
            checks["rust_perf_regression_gate"] = {
                "pass": False,
                "detail": "Skipped because rust_module is not declared in manifest",
            }
        return False, {"checks": checks, "rust_module": None}

    parity_path = Path(config.rust_parity_test_path)
    if not parity_path.is_absolute():
        parity_path = root / parity_path
    parity_cmd = ["uv", "run", "pytest", "-q", "--no-cov", str(parity_path)]
    try:
        parity_proc = subprocess.run(
            parity_cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=max(10, int(config.rust_parity_timeout_s)),
            check=False,
        )
        checks["rust_parity_tests"] = {
            "command": " ".join(parity_cmd),
            "returncode": int(parity_proc.returncode),
            "stdout_tail": parity_proc.stdout[-2000:],
            "stderr_tail": parity_proc.stderr[-2000:],
            "pass": parity_proc.returncode == 0,
        }
    except subprocess.TimeoutExpired as exc:
        checks["rust_parity_tests"] = {
            "command": " ".join(parity_cmd),
            "returncode": 124,
            "stdout_tail": (exc.stdout or "")[-2000:],
            "stderr_tail": (exc.stderr or "")[-2000:],
            "pass": False,
            "detail": f"timeout after {int(config.rust_parity_timeout_s)}s",
        }

    if bool(config.enforce_rust_benchmark_gate):
        bench_cmd = shlex.split(str(config.rust_benchmark_cmd))
        try:
            bench_proc = subprocess.run(
                bench_cmd,
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=max(10, int(config.rust_parity_timeout_s)),
                check=False,
            )
            checks["rust_perf_regression_gate"] = {
                "command": " ".join(bench_cmd),
                "returncode": int(bench_proc.returncode),
                "stdout_tail": bench_proc.stdout[-2000:],
                "stderr_tail": bench_proc.stderr[-2000:],
                "pass": bench_proc.returncode == 0,
            }
        except subprocess.TimeoutExpired as exc:
            checks["rust_perf_regression_gate"] = {
                "command": " ".join(bench_cmd),
                "returncode": 124,
                "stdout_tail": (exc.stdout or "")[-2000:],
                "stderr_tail": (exc.stderr or "")[-2000:],
                "pass": False,
                "detail": f"timeout after {int(config.rust_parity_timeout_s)}s",
            }

    passed = all(bool(v.get("pass", False)) for v in checks.values())
    return passed, {"checks": checks, "rust_module": rust_module}


def _build_paper_governance_report(
    config: PromotionConfig,
    gate_e_checks: dict[str, Any],
) -> dict[str, Any]:
    gate_e_map_raw = gate_e_checks.get("checks", {})
    gate_e_map = gate_e_map_raw if isinstance(gate_e_map_raw, dict) else {}
    summary_raw = gate_e_checks.get("paper_trade_summary")
    summary = summary_raw if isinstance(summary_raw, dict) else {}

    shadow_check = gate_e_map.get("shadow_sessions", {})
    drift_check = gate_e_map.get("drift_alerts", {})
    reject_check = gate_e_map.get("execution_reject_rate", {})

    session_count = int(summary.get("session_count", shadow_check.get("value", 0)))
    calendar_days = int(summary.get("calendar_span_days", 0))
    trading_days = int(summary.get("distinct_trading_days", 0))
    min_session_seconds = int(summary.get("min_session_duration_seconds", 0))
    invalid_duration_count = int(summary.get("invalid_session_duration_count", session_count))
    drift_alerts_total = int(summary.get("drift_alerts_total", drift_check.get("value", 0)))
    reject_rate = _to_float(reject_check.get("value")) or 0.0
    reject_rate_source = str(reject_check.get("source") or "mean")
    regimes_covered_raw = summary.get("regimes_covered", [])
    regimes_covered = [str(x) for x in regimes_covered_raw] if isinstance(regimes_covered_raw, list) else []
    required_session_seconds = max(60, int(config.min_paper_trade_session_minutes) * 60)
    min_required_regimes = 2

    checks: dict[str, dict[str, Any]] = {
        "shadow_sessions": {
            "value": session_count,
            "min": int(config.min_shadow_sessions),
            "pass": session_count >= int(config.min_shadow_sessions),
        },
        "calendar_span_days": {
            "value": calendar_days,
            "min": int(config.min_paper_trade_calendar_days),
            "pass": calendar_days >= int(config.min_paper_trade_calendar_days),
        },
        "trading_days": {
            "value": trading_days,
            "min": int(config.min_paper_trade_trading_days),
            "pass": trading_days >= int(config.min_paper_trade_trading_days),
        },
        "session_duration": {
            "value": min_session_seconds,
            "min_seconds": required_session_seconds,
            "invalid_session_duration_count": invalid_duration_count,
            "pass": (invalid_duration_count == 0 and min_session_seconds >= required_session_seconds),
        },
        "drift_alerts": {
            "value": drift_alerts_total,
            "max": 0,
            "pass": drift_alerts_total <= 0,
        },
        "execution_reject_rate": {
            "value": reject_rate,
            "max": float(config.max_execution_reject_rate),
            "source": reject_rate_source,
            "pass": reject_rate <= float(config.max_execution_reject_rate),
        },
        "regime_span": {
            "value": len(regimes_covered),
            "covered": regimes_covered,
            "min": min_required_regimes,
            "pass": len(regimes_covered) >= min_required_regimes,
        },
    }
    passed = all(bool(item.get("pass", False)) for item in checks.values())
    return {
        "alpha_id": config.alpha_id,
        "passed": passed,
        "checks": checks,
        "summary": (summary if summary else None),
        "paper_trade_summary_source": gate_e_checks.get("paper_trade_summary_source"),
        "paper_trade_summary_error": gate_e_checks.get("paper_trade_summary_error"),
    }


def _resolve_paper_trade_summary(
    config: PromotionConfig,
    root: Path,
) -> tuple[dict[str, Any] | None, str, str | None]:
    if config.paper_trade_summary_path:
        summary_path = Path(config.paper_trade_summary_path)
        if not summary_path.is_absolute():
            summary_path = root / summary_path
        if not summary_path.exists():
            return None, "explicit", f"paper_trade_summary_path_not_found:{summary_path}"
        try:
            payload = json.loads(summary_path.read_text())
        except (OSError, ValueError) as exc:
            return None, "explicit", f"paper_trade_summary_read_error:{exc}"
        if not isinstance(payload, dict):
            return None, "explicit", "paper_trade_summary_invalid_format"
        return payload, "explicit", None

    try:
        from hft_platform.alpha.experiments import ExperimentTracker

        base = root / config.experiments_dir
        tracker = ExperimentTracker(base_dir=base)
        summary = tracker.summarize_paper_trade(config.alpha_id)
        if not isinstance(summary, dict):
            return None, "tracker", "paper_trade_summary_invalid_format"
        if int(summary.get("session_count", 0)) <= 0:
            return None, "tracker", "paper_trade_sessions_missing"
        return summary, "tracker", None
    except Exception as exc:
        return None, "tracker", f"paper_trade_summary_tracker_error:{exc}"


def _load_rust_module_name(root: Path, alpha_id: str) -> str:
    try:
        from hft_platform.alpha.discovery import AlphaDiscoveryRegistry

        registry = AlphaDiscoveryRegistry()
        loaded = registry.discover(str(root / "research" / "alphas"))
        alpha = loaded.get(alpha_id)
        if alpha is None:
            return ""
        rust_module = getattr(alpha.manifest, "rust_module", None)
        return str(rust_module or "").strip()
    except Exception as _exc:  # noqa: BLE001
        return ""


def build_promotion_checklist(
    config: PromotionConfig,
    gate_d_checks: dict[str, Any],
    gate_e_checks: dict[str, Any],
    gate_f_checks: dict[str, Any] | None = None,
) -> PromotionChecklist:
    gate_e_map = gate_e_checks.get("checks", gate_e_checks)
    gate_f_map = (gate_f_checks or {}).get("checks", gate_f_checks or {})

    sharpe = gate_d_checks.get("sharpe_oos", {})
    drawdown = gate_d_checks.get("max_drawdown", {})
    turnover = gate_d_checks.get("turnover", {})
    shadow = gate_e_map.get("shadow_sessions", {})
    drift = gate_e_map.get("drift_alerts", {})
    reject = gate_e_map.get("execution_reject_rate", {})
    paper_log = gate_e_map.get("paper_trade_log_available", {})
    paper_span = gate_e_map.get("paper_trade_calendar_days", {})
    paper_days = gate_e_map.get("paper_trade_trading_days", {})
    paper_session_duration = gate_e_map.get("paper_trade_session_duration", {})

    latency = gate_d_checks.get("latency_profile", {})

    items = [
        PromotionChecklistItem(
            label="Gate D: OOS Sharpe >= threshold",
            passed=bool(sharpe.get("pass", False)),
            detail=f"sharpe_oos={sharpe.get('value')} (min={config.min_sharpe_oos})",
        ),
        PromotionChecklistItem(
            label="Gate D: Max drawdown within limit",
            passed=bool(drawdown.get("pass", False)),
            detail=f"max_drawdown={drawdown.get('value')} (min={-abs(config.max_abs_drawdown)})",
        ),
        PromotionChecklistItem(
            label="Gate D: Turnover within limit",
            passed=bool(turnover.get("pass", False)),
            detail=f"turnover={turnover.get('value')} (max={config.max_turnover})",
        ),
        PromotionChecklistItem(
            label="Gate D: Latency profile recorded (constitution requirement)",
            passed=bool(latency.get("pass", False)),
            detail=str(latency.get("detail", "latency_profile not checked")),
        ),
        PromotionChecklistItem(
            label="Gate E: Sufficient shadow sessions",
            passed=bool(shadow.get("pass", False)),
            detail=f"sessions={shadow.get('value')} (min={config.min_shadow_sessions})",
        ),
        PromotionChecklistItem(
            label="Gate E: No drift alerts",
            passed=bool(drift.get("pass", False)),
            detail=f"drift_alerts={drift.get('value')} (max=0)",
        ),
        PromotionChecklistItem(
            label="Gate E: Execution reject rate within limit",
            passed=bool(reject.get("pass", False)),
            detail=f"reject_rate={reject.get('value')} (max={config.max_execution_reject_rate})",
        ),
    ]
    if bool(config.require_paper_trade_governance):
        items.extend(
            [
                PromotionChecklistItem(
                    label="Gate E: Paper-trade log is available",
                    passed=bool(paper_log.get("pass", False)),
                    detail=f"source={paper_log.get('source')} error={paper_log.get('error')}",
                ),
                PromotionChecklistItem(
                    label="Gate E: Paper-trade calendar span >= 7 days",
                    passed=bool(paper_span.get("pass", False)),
                    detail=(
                        f"calendar_span_days={paper_span.get('value')} (min={config.min_paper_trade_calendar_days})"
                    ),
                ),
                PromotionChecklistItem(
                    label="Gate E: Paper-trade distinct trading days >= 5",
                    passed=bool(paper_days.get("pass", False)),
                    detail=(f"trading_days={paper_days.get('value')} (min={config.min_paper_trade_trading_days})"),
                ),
                PromotionChecklistItem(
                    label="Gate E: Paper-trade session duration governance",
                    passed=bool(paper_session_duration.get("pass", False)),
                    detail=(
                        f"min_session_seconds={paper_session_duration.get('value')} "
                        f"(min={int(config.min_paper_trade_session_minutes) * 60}, "
                        f"invalid={paper_session_duration.get('invalid_session_duration_count')})"
                    ),
                ),
            ]
        )
    if (gate_f_checks or {}).get("outcome") == Outcome.NOT_APPLICABLE.value:
        # The profile declared Gate F not applicable; there are no Rust checks to list.
        items.append(
            PromotionChecklistItem(
                label="Gate F: not applicable",
                passed=True,
                detail=str((gate_f_checks or {}).get("reason", "")),
            )
        )
    elif bool(config.enable_rust_readiness_gate):
        rust_module = gate_f_map.get("rust_module_declared", {})
        rust_parity = gate_f_map.get("rust_parity_tests", {})
        items.extend(
            [
                PromotionChecklistItem(
                    label="Gate F: Rust module declared in manifest",
                    passed=bool(rust_module.get("pass", False)),
                    detail=f"rust_module={rust_module.get('value')}",
                ),
                PromotionChecklistItem(
                    label="Gate F: Rust parity tests pass",
                    passed=bool(rust_parity.get("pass", False)),
                    detail=f"returncode={rust_parity.get('returncode')}",
                ),
            ]
        )
        if bool(config.enforce_rust_benchmark_gate):
            rust_bench = gate_f_map.get("rust_perf_regression_gate", {})
            items.append(
                PromotionChecklistItem(
                    label="Gate F: Rust performance regression gate pass",
                    passed=bool(rust_bench.get("pass", False)),
                    detail=f"returncode={rust_bench.get('returncode')}",
                )
            )
    return PromotionChecklist(items=items)


def _suggest_canary_weight(scorecard: dict[str, Any], override: float | None = None) -> float:
    if override is not None:
        return max(0.0, float(override))

    sharpe = _to_float(scorecard.get("sharpe_oos")) or 0.0
    if sharpe >= 2.0:
        return 0.10
    if sharpe >= 1.5:
        return 0.07
    if sharpe >= 1.0:
        return 0.05
    return 0.02


def _promotion_obligations(verdict: VerdictResult, config: PromotionConfig) -> list[dict[str, Any]]:
    """The deferred gates an approval owes, each with the threshold its evidence must meet."""
    obligations: list[dict[str, Any]] = []
    for gate, stage in verdict.obligations:
        entry: dict[str, Any] = {"gate": gate, "stage": stage}
        if gate == "replay_parity_audit":
            entry["min_match_pct"] = float(getattr(config, "min_replay_parity_match_pct", 95.0))
        obligations.append(entry)
    return obligations


def _write_promotion_config(
    root: Path,
    config: PromotionConfig,
    canary_weight: float,
    expiry_review_date: str,
    scorecard: dict[str, Any],
    approved: bool,
    forced: bool,
    obligations: list[dict[str, Any]] | None = None,
) -> Path:
    day = datetime.fromtimestamp(timebase.now_s(), tz=_dt.timezone.utc).strftime("%Y%m%d")
    if forced:
        # L9 (loop_v1): forced promotions are research-only escape hatches.
        # They MUST NOT live under config/strategy_promotions/ — that path is
        # consumed by live promotion config. CI guard blocks
        # config/live/** from referencing research/forced_promotions/.
        out = root / "research" / "forced_promotions" / day / f"{config.alpha_id}.yaml"
    else:
        out = root / "config" / "strategy_promotions" / day / f"{config.alpha_id}.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "alpha_id": config.alpha_id,
        "enabled": bool(approved) and not forced,
        "live_promotion_eligible": not forced,
        "weight": float(canary_weight) if not forced else 0.0,
        "owner": config.owner,
        "expiry_review_date": expiry_review_date,
        "forced": forced,
        "source_commit": _git_commit(root),
        "config_version": config.config_version,
        "parent_config_version": config.parent_config_version,
        "scorecard_snapshot": {
            "sharpe_oos": _to_float(scorecard.get("sharpe_oos")),
            "max_drawdown": _to_float(scorecard.get("max_drawdown")),
            "turnover": _to_float(scorecard.get("turnover")),
            "correlation_pool_max": _to_float(scorecard.get("correlation_pool_max")),
        },
        "thresholds": {
            "min_sharpe_oos": config.min_sharpe_oos,
            "max_abs_drawdown": config.max_abs_drawdown,
            "max_turnover": config.max_turnover,
            "max_correlation": config.max_correlation,
            "min_shadow_sessions": config.min_shadow_sessions,
            "min_paper_trade_calendar_days": config.min_paper_trade_calendar_days,
            "min_paper_trade_trading_days": config.min_paper_trade_trading_days,
            "min_paper_trade_session_minutes": int(config.min_paper_trade_session_minutes),
        },
        "guardrails": {
            "max_live_slippage_bps": config.max_live_slippage_bps,
            "max_live_drawdown_contribution": config.max_live_drawdown_contribution,
            "max_execution_error_rate": config.max_execution_error_rate,
        },
        "readiness": {
            "require_paper_trade_governance": bool(config.require_paper_trade_governance),
            "enable_rust_readiness_gate": bool(config.enable_rust_readiness_gate),
            "rust_module_name": config.rust_module_name,
        },
        # Deferred gates still owed (ADR-003); CanaryMonitor will not raise
        # the weight until each one carries passing evidence.
        "obligations": list(obligations or []),
        "rollback": {
            "trigger": {
                "live_slippage_bps_gt": config.max_live_slippage_bps,
                "live_drawdown_contribution_gt": config.max_live_drawdown_contribution,
                "execution_error_rate_gt": config.max_execution_error_rate,
            },
            "action": {
                "set_weight_to_zero": True,
                "open_incident": True,
            },
        },
    }
    out.write_text(yaml.safe_dump(payload, sort_keys=False))
    return out


def _git_commit(root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception as _exc:  # noqa: BLE001
        pass
    return "unknown"


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _kill_ledger_enabled() -> bool:
    """HFT_KILL_LEDGER_ENABLED gates the auto-kill writes (default 1=on)."""
    import os

    return os.getenv("HFT_KILL_LEDGER_ENABLED", "1") != "0"


def _load_manifest_artifact_hash(alpha_dir: Path) -> str:
    """Best-effort manifest load -> ``stable_artifact_hash``.

    Missing / unparseable manifest yields ``""`` so a kill row still gets
    written (degraded dedupe key -- same alpha+gate collapses regardless).
    """
    manifest_path = alpha_dir / "manifest.yaml"
    if not manifest_path.exists():
        return ""
    try:
        from hft_platform.alpha.kill_ledger import stable_artifact_hash
        from hft_platform.contracts.alpha import AlphaManifest

        payload = yaml.safe_load(manifest_path.read_text()) or {}
        if not isinstance(payload, dict):
            return ""
        manifest = AlphaManifest.from_dict(payload)
        return stable_artifact_hash(manifest)
    except Exception:  # noqa: BLE001 -- best-effort; degraded-key kill row is acceptable
        return ""


def _scorecard_run_id(scorecard_path: Path) -> str:
    """Run id from the ``meta.json`` beside a scorecard ("" when absent)."""
    meta_path = scorecard_path.parent / "meta.json"
    try:
        return str(json.loads(meta_path.read_text()).get("run_id") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def _auto_kill(
    *,
    alpha_id: str,
    gate: str,
    verdict: VerdictResult,
    run_id: str,
    alpha_dir: Path,
    scorecard_path: Path,
) -> None:
    """Best-effort write of a KILL verdict to the Slice-D kill ledger.

    The record is built by ``KillRecord.from_verdict``, which refuses any
    verdict but KILL. Failures here MUST NOT propagate -- the original
    promotion-rejection signal is what callers contract on. Any exception
    from the ledger write is logged at WARNING and swallowed.
    """
    if not _kill_ledger_enabled():
        return
    try:
        from hft_platform.alpha.kill_ledger import KillRecord, append_kill

        append_kill(
            KillRecord.from_verdict(
                alpha_id=alpha_id,
                gate=gate,
                verdict=verdict,
                run_id=run_id,
                stable_artifact_hash=_load_manifest_artifact_hash(alpha_dir),
                scorecard_id=str(scorecard_path),
                killed_by="promote_alpha:auto",
            )
        )
    except Exception:  # noqa: BLE001 -- best-effort; never block promotion error path
        _log.warning(
            "auto_kill_log_failed",
            alpha_id=alpha_id,
            gate=gate,
            exc_info=True,
        )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
