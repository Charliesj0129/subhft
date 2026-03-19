"""Canary live metrics writer for the alpha promotion pipeline.

Reads live metrics from ClickHouse (or a pluggable source), computes
canary scorecard metrics, and writes a ``live_metrics:`` block into the
alpha's promotion YAML config.  The write is idempotent — repeated calls
update the block in-place without touching other fields in the file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
import yaml

logger = structlog.get_logger("alpha.canary_metrics_writer")

# Metric fields written to the live_metrics YAML block.
_LIVE_METRIC_FIELDS = (
    "slippage_bps",
    "drawdown_contribution",
    "execution_error_rate",
    "sessions_live",
    "sharpe_live",
)


@dataclass
class LiveMetrics:
    """Computed live metrics for a canary alpha."""

    alpha_id: str
    slippage_bps: float = 0.0
    drawdown_contribution: float = 0.0
    execution_error_rate: float = 0.0
    sessions_live: int = 0
    sharpe_live: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "slippage_bps": self.slippage_bps,
            "drawdown_contribution": self.drawdown_contribution,
            "execution_error_rate": self.execution_error_rate,
            "sessions_live": self.sessions_live,
        }
        if self.sharpe_live is not None:
            d["sharpe_live"] = self.sharpe_live
        return d


@dataclass
class MetricsWriteResult:
    """Result of a single update-metrics run."""

    alpha_id: str
    yaml_path: str | None
    metrics: LiveMetrics | None
    updated: bool
    error: str | None = None


class CanaryMetricsWriter:
    """Fetch live alpha metrics and persist them into the promotion YAML.

    Args:
        promotions_dir: Directory containing per-alpha promotion YAML files
            (defaults to ``config/strategy_promotions``).
        clickhouse_client: Optional pre-configured ClickHouse client.  When
            ``None``, the ClickHouse fetch path returns empty metrics and
            falls back to zeros.
    """

    def __init__(
        self,
        promotions_dir: str = "config/strategy_promotions",
        clickhouse_client: Any = None,
    ) -> None:
        self.promotions_dir = Path(promotions_dir)
        self._ch_client = clickhouse_client

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, alpha_id: str) -> MetricsWriteResult:
        """Fetch live metrics and write them to the promotion YAML.

        Args:
            alpha_id: The alpha to update.

        Returns:
            A :class:`MetricsWriteResult` describing what happened.
        """
        yaml_path = self._find_promotion_yaml(alpha_id)
        if yaml_path is None:
            logger.warning(
                "canary_metrics_writer.no_yaml",
                alpha_id=alpha_id,
                promotions_dir=str(self.promotions_dir),
            )
            return MetricsWriteResult(
                alpha_id=alpha_id,
                yaml_path=None,
                metrics=None,
                updated=False,
                error=f"No promotion YAML found for {alpha_id!r} under {self.promotions_dir}",
            )

        try:
            raw_metrics = self._fetch_from_clickhouse(alpha_id)
            computed = self._compute_metrics(alpha_id, raw_metrics)
            self._write_metrics_to_yaml(yaml_path, computed)
            logger.info(
                "canary_metrics_writer.updated",
                alpha_id=alpha_id,
                yaml_path=str(yaml_path),
                metrics=computed.to_dict(),
            )
            return MetricsWriteResult(
                alpha_id=alpha_id,
                yaml_path=str(yaml_path),
                metrics=computed,
                updated=True,
            )
        except Exception as exc:
            logger.error(
                "canary_metrics_writer.error",
                alpha_id=alpha_id,
                error=str(exc),
                exc_info=True,
            )
            return MetricsWriteResult(
                alpha_id=alpha_id,
                yaml_path=str(yaml_path),
                metrics=None,
                updated=False,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # ClickHouse fetch
    # ------------------------------------------------------------------

    def _fetch_from_clickhouse(self, alpha_id: str) -> dict[str, Any]:
        """Query ClickHouse ``hft.alpha_trades`` for live slippage metrics.

        Computes:
        - ``avg_slippage_bps`` — average fill slippage in basis points
        - ``max_drawdown_contribution`` — worst per-session drawdown contribution
        - ``avg_error_rate`` — mean execution error rate
        - ``session_count`` — distinct session count
        - ``sharpe_live`` — annualised Sharpe of per-session PnL (when available)

        Returns an empty dict when the client is unavailable or the query
        returns no rows.
        """
        if self._ch_client is None:
            logger.debug("canary_metrics_writer.ch_skip", alpha_id=alpha_id, reason="no client")
            return {}

        query = (
            "SELECT "
            "  avg(slippage_bps)           AS avg_slippage_bps, "
            "  max(drawdown_contribution)  AS max_dd_contrib, "
            "  avg(execution_error_rate)   AS avg_error_rate, "
            "  count(*)                    AS session_count, "
            "  if(stddevPop(session_pnl) > 0, "
            "     avg(session_pnl) / stddevPop(session_pnl) * sqrt(252), "
            "     NULL)                   AS sharpe_live "
            "FROM hft.alpha_trades "
            "WHERE alpha_id = %(alpha_id)s"
        )
        try:
            rows = self._ch_client.execute(query, {"alpha_id": alpha_id})
        except Exception:
            logger.warning("canary_metrics_writer.ch_query_failed", alpha_id=alpha_id, exc_info=True)
            return {}

        if not rows:
            logger.debug("canary_metrics_writer.ch_no_rows", alpha_id=alpha_id)
            return {}

        row = rows[0]
        if len(row) < 4:
            logger.warning("canary_metrics_writer.ch_bad_shape", alpha_id=alpha_id, row=row)
            return {}

        avg_slip, max_dd, avg_err, session_count = row[0], row[1], row[2], row[3]
        sharpe_raw = row[4] if len(row) > 4 else None

        result: dict[str, Any] = {
            "slippage_bps": float(avg_slip) if avg_slip is not None else 0.0,
            "drawdown_contribution": float(max_dd) if max_dd is not None else 0.0,
            "execution_error_rate": float(avg_err) if avg_err is not None else 0.0,
            "sessions_live": int(session_count) if session_count is not None else 0,
        }
        if sharpe_raw is not None:
            try:
                result["sharpe_live"] = float(sharpe_raw)
            except (TypeError, ValueError):
                pass

        logger.debug("canary_metrics_writer.ch_fetched", alpha_id=alpha_id, raw=result)
        return result

    # ------------------------------------------------------------------
    # Metric computation
    # ------------------------------------------------------------------

    def _compute_metrics(self, alpha_id: str, raw: dict[str, Any]) -> LiveMetrics:
        """Construct a :class:`LiveMetrics` from raw fetch data.

        Applies type-safe coercions and guards against missing keys.
        Float is acceptable here (offline alpha module per architecture rule 11).
        """
        slippage_bps = float(raw.get("slippage_bps", 0.0))
        drawdown_contribution = float(raw.get("drawdown_contribution", 0.0))
        execution_error_rate = float(raw.get("execution_error_rate", 0.0))
        sessions_live_raw = raw.get("sessions_live", 0)
        sessions_live = int(sessions_live_raw) if sessions_live_raw is not None else 0
        sharpe_raw = raw.get("sharpe_live")
        sharpe_live: float | None = None
        if sharpe_raw is not None:
            try:
                sharpe_live = float(sharpe_raw)
            except (TypeError, ValueError):
                pass

        return LiveMetrics(
            alpha_id=alpha_id,
            slippage_bps=slippage_bps,
            drawdown_contribution=drawdown_contribution,
            execution_error_rate=execution_error_rate,
            sessions_live=sessions_live,
            sharpe_live=sharpe_live,
        )

    # ------------------------------------------------------------------
    # YAML I/O
    # ------------------------------------------------------------------

    def _find_promotion_yaml(self, alpha_id: str) -> Path | None:
        """Scan *promotions_dir* for the most-recent YAML for *alpha_id*.

        Returns the first matching path (sorted descending by name so the
        latest date-stamped directory wins), or ``None`` when not found.
        """
        if not self.promotions_dir.exists():
            return None

        matches: list[Path] = []
        for yaml_path in self.promotions_dir.rglob("*.yaml"):
            try:
                payload = yaml.safe_load(yaml_path.read_text())
            except Exception as exc:
                logger.debug("operation_fallback", error=str(exc))
                continue
            if isinstance(payload, dict) and payload.get("alpha_id") == alpha_id:
                matches.append(yaml_path)

        if not matches:
            return None

        # Return the lexicographically latest (newest date-stamped dir).
        return sorted(matches, reverse=True)[0]

    def _write_metrics_to_yaml(self, yaml_path: Path, metrics: LiveMetrics) -> None:
        """Merge *metrics* into the ``live_metrics:`` block of the YAML file.

        Uses ``yaml.safe_dump`` with ``sort_keys=False`` to preserve field
        order — consistent with the pattern in ``canary.py`` line 218.
        """
        try:
            existing = yaml.safe_load(yaml_path.read_text()) or {}
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
            existing = {}

        if not isinstance(existing, dict):
            existing = {}

        # Update (or create) the live_metrics block.
        existing["live_metrics"] = metrics.to_dict()

        yaml_path.write_text(yaml.safe_dump(existing, sort_keys=False))
        logger.debug(
            "canary_metrics_writer.yaml_written",
            path=str(yaml_path),
            alpha_id=metrics.alpha_id,
        )
