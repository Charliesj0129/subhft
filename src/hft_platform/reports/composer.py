"""Layer 3 -- ReportComposer: builds tier-aware MessageParts from facts + reasoning.

Combines FactReport (Layer 1) and ReasoningReport (Layer 2) into a ComposedReport
containing 8 text/image messages ready for distribution.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import structlog

from hft_platform.contracts.types import PLATFORM_SCALE
from hft_platform.reports.models import ComposedReport, MessagePart

if TYPE_CHECKING:
    from hft_platform.reports.models import (
        FactReport,
        ReasoningReport,
    )

__all__ = ["ReportComposer"]

log = structlog.get_logger(__name__)
TELEGRAM_MAX_LEN: Final[int] = 4096

_SESSION_LABELS: Final[dict[str, str]] = {
    "day": "日",
    "night": "夜",
}

_BIAS_LABELS: Final[dict[str, str]] = {
    "bullish": "偏多",
    "bearish": "偏空",
    "neutral": "中性",
}

_PRICE_POSITION_DESC: Final[dict[str, str]] = {
    "above_prev_high": "收盤高於前日高點",
    "below_prev_low": "收盤低於前日低點",
    "inside_range": "收盤在前日區間內",
}

_TREND_DESC: Final[dict[str, str]] = {
    "up": "連續上漲",
    "down": "連續下跌",
    "sideways": "盤整",
}


# ---------------------------------------------------------------------------
# Formatting helpers (ported from renderer.py)
# ---------------------------------------------------------------------------


def _p(scaled: int) -> str:
    """Format scaled price as human-readable with commas."""
    return f"{scaled // PLATFORM_SCALE:,}"


def _pct(open_p: int, close_p: int) -> str:
    """Format price change like '▲333 (+1.63%)' or '▼167 (-0.82%)'."""
    diff = close_p - open_p
    pct = diff / open_p * 100 if open_p else 0.0
    arrow = "▲" if diff >= 0 else "▼"
    return f"{arrow}{abs(diff // PLATFORM_SCALE)} ({pct:+.2f}%)"


def _stars(n: int) -> str:
    """Convert importance 1-3 to star display."""
    filled = min(max(n, 0), 3)
    return "★" * filled + "☆" * (3 - filled)


def _ud_bar(ratio: float) -> str:
    """Return a short textual bar representing U/D ratio."""
    if ratio >= 2.0:
        return "█████ 強多"
    if ratio >= 1.5:
        return "████░ 多"
    if ratio >= 1.1:
        return "███░░ 偏多"
    if ratio >= 0.9:
        return "██░░░ 平衡"
    if ratio >= 0.67:
        return "█░░░░ 偏空"
    if ratio >= 0.5:
        return "▌░░░░ 空"
    return "░░░░░ 強空"


def _median_spread(spread_dist: dict[int, int]) -> int:
    """Return weighted median spread in pts from spread_dist."""
    if not spread_dist:
        return 0
    total = sum(spread_dist.values())
    mid = total / 2
    cumulative = 0
    for pts in sorted(spread_dist):
        cumulative += spread_dist[pts]
        if cumulative >= mid:
            return pts
    return 0


# ---------------------------------------------------------------------------
# Message splitting
# ---------------------------------------------------------------------------


def _split_message(content: str, min_tier: str) -> list[MessagePart]:
    """Split a text message into parts that each fit within TELEGRAM_MAX_LEN."""
    if len(content) <= TELEGRAM_MAX_LEN:
        return [MessagePart(kind="text", content=content, min_tier=min_tier)]

    parts: list[MessagePart] = []
    remaining = content
    while len(remaining) > TELEGRAM_MAX_LEN:
        # Find last newline before the limit
        cut = remaining.rfind("\n", 0, TELEGRAM_MAX_LEN)
        if cut <= 0:
            # No newline found; hard cut at limit
            cut = TELEGRAM_MAX_LEN
        parts.append(MessagePart(kind="text", content=remaining[:cut], min_tier=min_tier))
        remaining = remaining[cut:].lstrip("\n")

    if remaining:
        parts.append(MessagePart(kind="text", content=remaining, min_tier=min_tier))
    return parts


# ---------------------------------------------------------------------------
# ReportComposer
# ---------------------------------------------------------------------------


class ReportComposer:
    """Build tier-aware MessageParts from FactReport + ReasoningReport."""

    def compose(self, fr: FactReport, rr: ReasoningReport) -> ComposedReport:
        """Compose all message parts into a ComposedReport."""
        parts: list[MessagePart] = []

        parts.extend(_split_message(self._compose_summary(fr, rr), "free"))
        parts.extend(_split_message(self._compose_narrative(rr), "paid"))
        parts.extend(_split_message(self._compose_flow(fr), "paid"))
        parts.extend(_split_message(self._compose_chips(fr), "paid"))
        parts.extend(_split_message(self._compose_levels(rr), "paid"))
        parts.extend(_split_message(self._compose_scenarios(rr), "paid"))

        heatmap = self._compose_heatmap(fr)
        if heatmap is not None:
            parts.append(heatmap)

        parts.extend(_split_message(self._compose_disclaimer(), "free"))

        log.info(
            "compose_done",
            symbol=fr.session_data.symbol,
            date=fr.session_data.date,
            message_count=len(parts),
        )
        return ComposedReport(messages=parts)

    # ------------------------------------------------------------------
    # 1. Summary (free tier)
    # ------------------------------------------------------------------

    def _compose_summary(self, fr: FactReport, rr: ReasoningReport) -> str:
        sd = fr.session_data
        session_label = _SESSION_LABELS.get(sd.session, sd.session)
        change = _pct(sd.open, sd.close)
        median_pts = _median_spread(sd.spread_dist)

        bias_label = _BIAS_LABELS.get(rr.bias.bias, "中性")
        confidence_pct = int(rr.bias.confidence * 100)

        # Top 4 evidences by weight
        sorted_ev = sorted(rr.bias.evidences, key=lambda e: e.weight, reverse=True)[:4]
        evidence_lines: list[str] = []
        for i, ev in enumerate(sorted_ev):
            prefix = "└" if i == len(sorted_ev) - 1 else "├"
            evidence_lines.append(f"{prefix} {ev.fact_value}")

        lines = [
            f"📊 台指期{session_label}盤報告 {sd.date}",
            "",
            f"{sd.symbol}  {_p(sd.open)} → {_p(sd.close)}  {change}",
            f"High {_p(sd.high)} | Low {_p(sd.low)} | Vol {sd.volume:,}",
            f"Ticks {sd.tick_count:,} | Spread 中位數 {median_pts}pts",
            "",
            f"偏向：{bias_label} (信心 {confidence_pct}%)",
        ]
        lines.extend(evidence_lines)

        # Cross-day comparison
        if fr.cross_day.prev_days:
            price_pos_desc = _PRICE_POSITION_DESC.get(
                fr.cross_day.price_position, fr.cross_day.price_position
            )
            cross_day_line = f"vs 前日：{price_pos_desc}"
            if fr.cross_day.flow_reversal:
                cross_day_line += "，流向反轉"
            lines.append("")
            lines.append(cross_day_line)

            trend_desc = _TREND_DESC.get(
                fr.cross_day.trend_direction, fr.cross_day.trend_direction
            )
            lines.append(f"vs 前 3 日：{trend_desc}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 2. Narrative (paid tier)
    # ------------------------------------------------------------------

    def _compose_narrative(self, rr: ReasoningReport) -> str:
        narr = rr.narrative
        lines = ["📖 時段敘事", ""]
        lines.append("\n\n".join(narr.storyline))

        if narr.turning_points:
            lines.append("")
            lines.append("轉折點：")
            for tp_name, tp_desc in narr.turning_points:
                lines.append(f"  {tp_name}: {tp_desc}")

        lines.append("")
        lines.append(f"→ {narr.conclusion}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 3. Flow analysis (paid tier)
    # ------------------------------------------------------------------

    def _compose_flow(self, fr: FactReport) -> str:
        flow = fr.flow

        lines = [
            "🔍 流向深度分析",
            "",
            f"▎全場 U/D = {flow.session_ud:.3f}  淨流 {flow.session_net_flow:+,} 口",
            f"▎最強多方: {flow.strongest_buy_bar.ts} U/D={flow.strongest_buy_bar.ud_ratio:.2f}",
            f"▎最強空方: {flow.strongest_sell_bar.ts} U/D={flow.strongest_sell_bar.ud_ratio:.2f}",
            "",
            "▎時段分析:",
        ]

        # Segments
        for seg in fr.segments:
            vol_pct = int(seg.volume_pct * 100)
            lines.append(
                f"  {seg.name} {seg.time_range} "
                f"{_ud_bar(seg.ud_ratio)} {vol_pct}%"
            )

        # Sustained runs
        if flow.sustained_runs:
            run_parts = [f"{side}x{count}({tr})" for side, count, tr in flow.sustained_runs]
            lines.append(f"▎持續壓力: {', '.join(run_parts)}")
        else:
            lines.append("▎持續壓力: 無")

        # EOD drift
        lines.append(
            f"▎尾盤漂移: U/D {flow.session_ud:.2f} → {flow.eod_ud:.2f} "
            f"(drift {flow.eod_drift:+.2f})"
        )

        # Volume spikes
        if flow.volume_spikes:
            lines.append("")
            for bar, ratio in flow.volume_spikes:
                lines.append(f"  爆量 {bar.ts} ({ratio:.1f}x)")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 4. Chips (paid tier)
    # ------------------------------------------------------------------

    def _compose_chips(self, fr: FactReport) -> str:
        chips = fr.chips

        buy_vol = chips.total_buy_volume
        sell_vol = chips.total_sell_volume
        if buy_vol > sell_vol:
            ratio_desc = f"買超 {buy_vol - sell_vol:,} 口"
        elif sell_vol > buy_vol:
            ratio_desc = f"賣超 {sell_vol - buy_vol:,} 口"
        else:
            ratio_desc = "均衡"

        buy_zone_str = (
            f"{_p(chips.buy_zone[0])}-{_p(chips.buy_zone[1])}"
            if chips.buy_zone is not None
            else "無明顯集中"
        )
        sell_zone_str = (
            f"{_p(chips.sell_zone[0])}-{_p(chips.sell_zone[1])}"
            if chips.sell_zone is not None
            else "無明顯集中"
        )

        lines = [
            "🏦 籌碼結構",
            "",
            f"▎大單: 買 {buy_vol} 口 / 賣 {sell_vol} 口 (淨{ratio_desc})",
            f"▎買方活動區: {buy_zone_str}",
            f"▎賣方活動區: {sell_zone_str}",
        ]

        if chips.clusters:
            lines.append("")
            lines.append("▎群聚:")
            for cl in chips.clusters:
                lines.append(
                    f"  {_p(cl.price_center)} {cl.dominant_side} "
                    f"{cl.trade_count}筆 {cl.time_range}"
                )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 5. Levels (paid tier)
    # ------------------------------------------------------------------

    def _compose_levels(self, rr: ReasoningReport) -> str:
        levels = rr.levels

        resistances = [lv for lv in levels if lv.side == "resistance"]
        pivots = [lv for lv in levels if lv.side == "pivot"]
        supports = [lv for lv in levels if lv.side == "support"]

        lines = ["🎯 關鍵點位"]

        if resistances:
            lines.append("")
            lines.append("▎壓力:")
            for i, lv in enumerate(resistances, 1):
                importance = _importance_from_strength(lv.strength)
                sources_str = ", ".join(lv.sources)
                lines.append(f"  R{i} {_p(lv.price)} {_stars(importance)} {sources_str}")

        if pivots:
            lines.append("")
            lines.append("▎攻防關鍵:")
            for i, lv in enumerate(pivots, 1):
                importance = _importance_from_strength(lv.strength)
                sources_str = ", ".join(lv.sources)
                lines.append(f"  P{i} {_p(lv.price)} {_stars(importance)} {sources_str}")

        if supports:
            lines.append("")
            lines.append("▎支撐:")
            for i, lv in enumerate(supports, 1):
                importance = _importance_from_strength(lv.strength)
                sources_str = ", ".join(lv.sources)
                lines.append(f"  S{i} {_p(lv.price)} {_stars(importance)} {sources_str}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 6. Scenarios (paid tier)
    # ------------------------------------------------------------------

    def _compose_scenarios(self, rr: ReasoningReport) -> str:
        labels = ["A", "B", "C", "D", "E"]
        lines = ["📋 情境規劃"]

        for idx, sc in enumerate(rr.scenarios):
            label = labels[idx] if idx < len(labels) else str(idx + 1)
            lines.append("")
            lines.append(f"【情境 {label}】{sc.label} — 機率{sc.probability}")
            lines.append(f"  觸發: {sc.condition}")
            if sc.target > 0:
                lines.append(f"  → 目標 {_p(sc.target)}")
            lines.append(f"  {sc.description}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 7. Heatmap (paid tier, image)
    # ------------------------------------------------------------------

    def _compose_heatmap(self, fr: FactReport) -> MessagePart | None:
        try:
            from hft_platform.reports.heatmap import generate_heatmap
        except ImportError:
            log.warning("heatmap module unavailable; skipping")
            return None

        image_bytes = generate_heatmap(fr.session_data)
        if image_bytes is None:
            return None

        return MessagePart(
            kind="image",
            content="",
            image=image_bytes,
            caption="流向熱力圖",
            min_tier="paid",
        )

    # ------------------------------------------------------------------
    # 8. Disclaimer (free tier)
    # ------------------------------------------------------------------

    def _compose_disclaimer(self) -> str:
        return "⚠️ 本報告基於歷史行情數據自動生成，僅供參考，不構成投資建議。投資有風險，請自行評估。"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _importance_from_strength(strength: float) -> int:
    """Map strength [0, 1] to importance 1-3."""
    if strength >= 0.8:
        return 3
    if strength >= 0.5:
        return 2
    return 1
