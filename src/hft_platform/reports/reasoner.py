"""Layer 2 — Reasoner: evidence-based bias, level enrichment, scenarios, narrative.

Four reasoners transform Layer 1 FactReport into Layer 2 ReasoningReport:
- BiasReasoner: weighted evidence aggregation for market direction
- LevelReasoner: confluence-based support/resistance enrichment
- ScenarioReasoner: conditional scenario generation with targets/stops
- NarrativeReasoner: time-segment narrative with turning points
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from hft_platform.reports.models import (
    BiasJudgment,
    EnrichedLevel,
    Evidence,
    NarrativeReport,
    PriceLevel,
    ReasoningReport,
    Scenario,
)

if TYPE_CHECKING:
    from hft_platform.reports.models import FactReport

__all__ = [
    "BiasReasoner",
    "LevelReasoner",
    "ScenarioReasoner",
    "NarrativeReasoner",
    "reason_all",
]

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# 1. BiasReasoner
# ---------------------------------------------------------------------------

# Buffer for proximity grouping in LevelReasoner (5 points = 50,000 scaled)
_LEVEL_PROXIMITY = 50_000

# Minimum gap to declare directional bias (avoid noise)
_BIAS_GAP = 0.10


def _threshold_dir(value: float, *, bull_above: float, bear_below: float) -> str:
    """Classify a numeric value into bull/bear/neutral by thresholds."""
    if value > bull_above:
        return "bull"
    if value < bear_below:
        return "bear"
    return "neutral"


class BiasReasoner:
    """Determine market bias from 8 weighted evidence sources."""

    def judge(self, fr: FactReport) -> BiasJudgment:
        """Evaluate bias from FactReport using weighted evidence chain."""
        evidences = self._collect_evidences(fr)

        # Aggregate
        bull_weight = 0.0
        bear_weight = 0.0
        total_weight = 0.0
        for ev in evidences:
            w = ev.weight
            if ev.direction == "neutral":
                total_weight += w * 0.5
            elif ev.direction == "bull":
                bull_weight += w
                total_weight += w
            elif ev.direction == "bear":
                bear_weight += w
                total_weight += w

        if bull_weight > bear_weight + _BIAS_GAP:
            bias = "bullish"
        elif bear_weight > bull_weight + _BIAS_GAP:
            bias = "bearish"
        else:
            bias = "neutral"

        confidence = max(bull_weight, bear_weight) / total_weight if total_weight > 0 else 0.0
        summary = self._build_summary(bias, confidence, fr)

        return BiasJudgment(
            bias=bias,
            confidence=confidence,
            evidences=evidences,
            summary=summary,
        )

    def _collect_evidences(self, fr: FactReport) -> list[Evidence]:  # noqa: C901
        """Collect all 8 evidence sources from the FactReport."""
        evidences: list[Evidence] = []

        # 1. flow.session_ud (weight 0.20)
        ud = fr.flow.session_ud
        evidences.append(
            Evidence(
                source="flow.session_ud",
                fact_value=f"{ud:.2f}",
                direction=_threshold_dir(ud, bull_above=1.15, bear_below=0.85),
                weight=0.20,
            )
        )

        # 2. flow.eod_drift (weight 0.15)
        drift = fr.flow.eod_drift
        evidences.append(
            Evidence(
                source="flow.eod_drift",
                fact_value=f"{drift:+.2f}",
                direction=_threshold_dir(drift, bull_above=0.20, bear_below=-0.20),
                weight=0.15,
            )
        )

        # 3. flow.sustained_runs (weight 0.15)
        evidences.append(self._eval_sustained_runs(fr))

        # 4. chips.net_ratio (weight 0.20)
        nr = fr.chips.net_ratio
        evidences.append(
            Evidence(
                source="chips.net_ratio",
                fact_value=f"{nr:.2f}",
                direction=_threshold_dir(nr, bull_above=0.57, bear_below=0.43),
                weight=0.20,
            )
        )

        # 5. segments.closing (weight 0.10)
        evidences.append(self._eval_closing_segment(fr))

        # 6. cross_day.trend (weight 0.10)
        trend = fr.cross_day.trend_direction
        direction = {"up": "bull", "down": "bear"}.get(trend, "neutral")
        evidences.append(
            Evidence(
                source="cross_day.trend",
                fact_value=trend,
                direction=direction,
                weight=0.10,
            )
        )

        # 7. cross_day.flow_reversal (weight 0.05)
        evidences.append(self._eval_flow_reversal(fr))

        # 8. structure.failed_breakouts (weight 0.05)
        evidences.append(self._eval_failed_breakouts(fr))

        return evidences

    @staticmethod
    def _eval_sustained_runs(fr: FactReport) -> Evidence:
        runs = fr.flow.sustained_runs
        has_bull = any(side == "bull" and count >= 4 for side, count, _ in runs)
        has_bear = any(side == "bear" and count >= 4 for side, count, _ in runs)
        if has_bull and not has_bear:
            direction = "bull"
        elif has_bear and not has_bull:
            direction = "bear"
        else:
            direction = "neutral"
        desc = ", ".join(f"{s}x{c}" for s, c, _ in runs) if runs else "none"
        return Evidence(source="flow.sustained_runs", fact_value=desc, direction=direction, weight=0.15)

    @staticmethod
    def _eval_closing_segment(fr: FactReport) -> Evidence:
        closing_seg = next((seg for seg in fr.segments if seg.name == "closing"), None)
        if closing_seg is not None and closing_seg.dominant_side in ("bull", "bear"):
            direction = closing_seg.dominant_side
        else:
            direction = "neutral"
        val = closing_seg.dominant_side if closing_seg else "n/a"
        return Evidence(source="segments.closing", fact_value=val, direction=direction, weight=0.10)

    @staticmethod
    def _eval_flow_reversal(fr: FactReport) -> Evidence:
        reversal = fr.cross_day.flow_reversal
        if reversal:
            today_ud = fr.flow.session_ud
            direction = _threshold_dir(today_ud, bull_above=1.05, bear_below=0.95)
        else:
            direction = "neutral"
        return Evidence(source="cross_day.flow_reversal", fact_value=str(reversal), direction=direction, weight=0.05)

    @staticmethod
    def _eval_failed_breakouts(fr: FactReport) -> Evidence:
        fbs = fr.structure.failed_breakouts
        has_support = any("支撐" in fb.reason for fb in fbs)
        has_resist = any("壓力" in fb.reason for fb in fbs)
        if has_support and not has_resist:
            direction = "bull"
        elif has_resist and not has_support:
            direction = "bear"
        else:
            direction = "neutral"
        desc = ", ".join(fb.reason for fb in fbs) if fbs else "none"
        return Evidence(source="structure.failed_breakouts", fact_value=desc, direction=direction, weight=0.05)

    def _build_summary(self, bias: str, confidence: float, fr: FactReport) -> str:
        """Build a one-line Chinese summary string."""
        bias_label = {"bullish": "偏多", "bearish": "偏空", "neutral": "中性"}.get(bias, "中性")
        pct = int(confidence * 100)

        parts: list[str] = []

        ud = fr.flow.session_ud
        if ud > 1.15:
            parts.append(f"全場 U/D={ud:.2f} 多方主導")
        elif ud < 0.85:
            parts.append(f"全場 U/D={ud:.2f} 空方主導")

        drift = fr.flow.eod_drift
        if drift > 0.20:
            parts.append("尾盤加壓")
        elif drift < -0.20:
            parts.append("尾盤轉弱")

        if fr.cross_day.trend_direction == "up":
            parts.append("連續上漲趨勢")
        elif fr.cross_day.trend_direction == "down":
            parts.append("連續下跌趨勢")

        detail = " + ".join(parts) if parts else "多空拉鋸"
        return f"{bias_label} ({pct}%): {detail}"


# ---------------------------------------------------------------------------
# 2. LevelReasoner
# ---------------------------------------------------------------------------


class LevelReasoner:
    """Collect, merge, and classify price levels with confluence scoring."""

    def analyze(self, fr: FactReport) -> list[EnrichedLevel]:
        """Collect all PriceLevels, group by proximity, classify, and filter."""
        raw_levels: list[PriceLevel] = []

        # Structure sources
        raw_levels.extend(fr.structure.double_bottoms)
        raw_levels.extend(fr.structure.double_tops)
        raw_levels.extend(fr.structure.failed_breakouts)
        raw_levels.extend(fr.structure.round_numbers)
        raw_levels.append(fr.structure.session_high)
        raw_levels.append(fr.structure.session_low)

        # Chip sources: VAP peaks
        raw_levels.extend(fr.chips.vap_peaks)

        # Chip sources: clusters → PriceLevel
        for cluster in fr.chips.clusters:
            strength = min(1.0, cluster.trade_count / 10.0)
            raw_levels.append(
                PriceLevel(
                    price=cluster.price_center,
                    strength=strength,
                    reason=f"大單群聚 {cluster.dominant_side}",
                )
            )

        # Group by proximity
        groups = self._group_by_proximity(raw_levels, _LEVEL_PROXIMITY)

        close = fr.session_data.close

        enriched: list[EnrichedLevel] = []
        for group in groups:
            # Merge: price = weighted avg by strength, strength = max
            total_s = sum(lv.strength for lv in group)
            if total_s > 0:
                price = int(sum(lv.price * lv.strength for lv in group) / total_s)
            else:
                price = group[0].price
            strength = max(lv.strength for lv in group)
            sources = [lv.reason for lv in group]
            confluence = len(group)

            # Classify side
            if price > close + _LEVEL_PROXIMITY:
                side = "resistance"
            elif price < close - _LEVEL_PROXIMITY:
                side = "support"
            else:
                side = "pivot"

            enriched.append(
                EnrichedLevel(
                    price=price,
                    side=side,
                    strength=strength,
                    sources=sources,
                    confluence_count=confluence,
                )
            )

        # Filter: confluence >= 2 OR (confluence == 1 AND strength >= 0.7)
        filtered = [
            lv for lv in enriched if lv.confluence_count >= 2 or (lv.confluence_count == 1 and lv.strength >= 0.7)
        ]

        # Sort by strength descending within each side
        filtered.sort(key=lambda lv: (-_side_order(lv.side), -lv.strength))

        return filtered

    @staticmethod
    def _group_by_proximity(
        levels: list[PriceLevel],
        proximity: int,
    ) -> list[list[PriceLevel]]:
        """Group levels within +-proximity of each other."""
        if not levels:
            return []

        sorted_levels = sorted(levels, key=lambda lv: lv.price)
        groups: list[list[PriceLevel]] = [[sorted_levels[0]]]

        for lv in sorted_levels[1:]:
            # Compare with the first element of the current group (anchor)
            if abs(lv.price - groups[-1][0].price) <= proximity:
                groups[-1].append(lv)
            else:
                groups.append([lv])

        return groups


def _side_order(side: str) -> int:
    """Sort order: support first, then pivot, then resistance."""
    return {"support": 0, "pivot": 1, "resistance": 2}.get(side, 3)


# ---------------------------------------------------------------------------
# 3. ScenarioReasoner
# ---------------------------------------------------------------------------


class ScenarioReasoner:
    """Generate conditional scenarios from bias, levels, and facts."""

    def generate(
        self,
        fr: FactReport,
        bias: BiasJudgment,
        levels: list[EnrichedLevel],
    ) -> list[Scenario]:
        """Produce scenarios whose trigger conditions are satisfied."""
        scenarios: list[Scenario] = []
        atr = fr.volatility.atr_session

        supports = [lv for lv in levels if lv.side == "support"]
        resistances = [lv for lv in levels if lv.side == "resistance"]

        # S1/R1 helpers
        s1 = supports[0].price if supports else None
        r1 = resistances[0].price if resistances else None
        close = fr.session_data.close

        prev_low = fr.cross_day.prev_days[0].low if fr.cross_day.prev_days else close - atr
        prev_high = fr.cross_day.prev_days[0].high if fr.cross_day.prev_days else close + atr
        prev_close = fr.cross_day.prev_days[0].close if fr.cross_day.prev_days else close

        # 1. break_below: support exists + bias != "bullish"
        if supports and bias.bias != "bullish":
            target = min(prev_low, s1 - atr) if s1 is not None else prev_low
            stop = s1 + atr // 2 if s1 is not None else close + atr // 2
            prob = self._probability(bias, "bearish")
            scenarios.append(
                Scenario(
                    id="break_below",
                    label="破底加速",
                    probability=prob,
                    condition=f"跌破支撐 {s1}",
                    target=target,
                    description=f"若跌破 S1 支撐，目標 {target}，停損 {stop}。偏空信心 {int(bias.confidence * 100)}%。",
                )
            )

        # 2. hold_bounce: support exists + any bull evidence
        has_bull_ev = any(ev.direction == "bull" for ev in bias.evidences)
        if supports and has_bull_ev:
            target = r1 if r1 is not None else prev_high
            stop = s1 - atr // 2 if s1 is not None else close - atr // 2
            prob = self._probability(bias, "bullish")
            scenarios.append(
                Scenario(
                    id="hold_bounce",
                    label="守支撐反彈",
                    probability=prob,
                    condition=f"守住支撐 {s1}，多方反彈",
                    target=target,
                    description=f"支撐守住後反彈，目標 {target}，停損 {stop}。",
                )
            )

        # 3. trend_continue: cross_day trend >= 2 days same + bias concordant
        trend = fr.cross_day.trend_direction
        trend_days = len(fr.cross_day.prev_days)
        bias_concordant = (trend == "up" and bias.bias == "bullish") or (trend == "down" and bias.bias == "bearish")
        if trend in ("up", "down") and trend_days >= 2 and bias_concordant:
            if trend == "up":
                target = close + int(1.5 * atr)
                stop = close - atr // 2
            else:
                target = close - int(1.5 * atr)
                stop = close + atr // 2
            prob = self._probability(bias, bias.bias)
            scenarios.append(
                Scenario(
                    id="trend_continue",
                    label="趨勢延續",
                    probability=prob,
                    condition=f"連續 {trend_days} 日{_trend_label(trend)}",
                    target=target,
                    description=f"趨勢延續，目標 {target}，停損 {stop}。",
                )
            )

        # 4. gap_fill: |open - prev_close| / prev_close >= 0.003
        open_price = fr.session_data.open
        if prev_close > 0:
            gap_ratio = abs(open_price - prev_close) / prev_close
        else:
            gap_ratio = 0.0
        if gap_ratio >= 0.003:
            gap_side = "up" if open_price > prev_close else "down"
            if gap_side == "up":
                stop = open_price + atr // 2
            else:
                stop = open_price - atr // 2
            prob = "中"
            scenarios.append(
                Scenario(
                    id="gap_fill",
                    label="跳空回補",
                    probability=prob,
                    condition=f"跳空 {gap_ratio:.1%}",
                    target=prev_close,
                    description=f"跳空{'上' if gap_side == 'up' else '下'}回補至前收 {prev_close}，停損 {stop}。",
                )
            )

        # 5. range_bound: range_atr_ratio < 0.7 + bias neutral
        if fr.volatility.range_atr_ratio < 0.7 and bias.bias == "neutral":
            high = fr.session_data.high
            low = fr.session_data.low
            stop_buf = int(0.3 * atr) if atr > 0 else 0
            scenarios.append(
                Scenario(
                    id="range_bound",
                    label="區間震盪",
                    probability="中",
                    condition=f"區間/ATR={fr.volatility.range_atr_ratio:.2f} < 0.7",
                    target=high,
                    description=f"區間震盪 {low}-{high}，突破停損 ±{stop_buf}。",
                )
            )

        return scenarios

    @staticmethod
    def _probability(bias: BiasJudgment, concordant_direction: str) -> str:
        """Derive probability label from bias concordance."""
        if bias.bias == concordant_direction and bias.confidence > 0.6:
            return "高"
        if bias.bias == concordant_direction:
            return "中"
        return "低"


def _trend_label(trend: str) -> str:
    return "上漲" if trend == "up" else "下跌"


# ---------------------------------------------------------------------------
# 4. NarrativeReasoner
# ---------------------------------------------------------------------------


class NarrativeReasoner:
    """Generate time-segment narrative with turning points and conclusion."""

    def narrate(self, fr: FactReport) -> NarrativeReport:
        """Build narrative paragraphs, turning points, and conclusion."""
        storyline: list[str] = []
        turning_points: list[tuple[str, str]] = []

        prev_side: str | None = None
        for seg in fr.segments:
            paragraph = self._segment_paragraph(seg)
            storyline.append(paragraph)

            # Detect turning points
            if prev_side is not None and prev_side != seg.dominant_side:
                if (prev_side == "bull" and seg.dominant_side == "bear") or (
                    prev_side == "bear" and seg.dominant_side == "bull"
                ):
                    turning_points.append(
                        (
                            seg.name,
                            f"{_side_chinese(prev_side)}→{_side_chinese(seg.dominant_side)}",
                        )
                    )
            prev_side = seg.dominant_side

        conclusion = self._build_conclusion(fr)

        return NarrativeReport(
            storyline=storyline,
            turning_points=turning_points,
            conclusion=conclusion,
        )

    @staticmethod
    def _segment_paragraph(seg: FactReport | None = None, **kwargs: object) -> str:
        """Build a paragraph for one segment.

        Accepts a SegmentFact positionally (the first arg after self in the
        outer method) or via **kwargs for testing flexibility.
        """
        # Accept SegmentFact directly
        from hft_platform.reports.models import SegmentFact

        if isinstance(seg, SegmentFact):
            s = seg
        else:
            raise TypeError("Expected SegmentFact")

        # Dominant description
        if s.dominant_side == "bull":
            dominant_desc = "多方主導"
        elif s.dominant_side == "bear":
            dominant_desc = "空方主導"
        else:
            dominant_desc = "多空拉鋸"

        # Volume description
        vol_pct = s.volume_pct
        if vol_pct > 0.35:
            vol_desc = f"佔全場 {vol_pct:.0%}（量能集中）"
        elif vol_pct < 0.15:
            vol_desc = f"佔全場 {vol_pct:.0%}（量能萎縮）"
        else:
            vol_desc = f"佔全場 {vol_pct:.0%}"

        # Large trade note
        large_total = s.large_buy_count + s.large_sell_count
        if large_total > 0:
            large_note = f"大單 {large_total} 筆（買 {s.large_buy_count}/賣 {s.large_sell_count}）"
        else:
            large_note = ""

        parts = [
            f"{s.name}（{s.time_range}）：{dominant_desc}，U/D={s.ud_ratio:.2f}，量能{vol_desc}",
        ]
        if large_note:
            parts[0] += f"。{large_note}"

        return parts[0]

    def _build_conclusion(self, fr: FactReport) -> str:
        """Build conclusion from last segment + cross_day trend."""
        if not fr.segments:
            return "無盤中資料"

        last = fr.segments[-1]
        side_cn = _side_chinese(last.dominant_side)

        trend = fr.cross_day.trend_direction
        trend_days = len(fr.cross_day.prev_days)

        if trend in ("up", "down") and trend_days >= 1:
            trend_cn = "走強" if trend == "up" else "走弱"
            return f"{side_cn}尾盤接管，連續第{trend_days + 1}日{trend_cn}"

        return f"{side_cn}尾盤接管，量能{_vol_comment(last.volume_pct)}"


def _side_chinese(side: str) -> str:
    return {"bull": "多方", "bear": "空方"}.get(side, "中性")


def _vol_comment(pct: float) -> str:
    if pct > 0.35:
        return "集中在尾盤"
    if pct < 0.15:
        return "萎縮"
    return "正常"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def reason_all(fr: FactReport) -> ReasoningReport:
    """Run all four reasoners and return a complete ReasoningReport."""
    log.info(
        "reasoning_start",
        symbol=fr.session_data.symbol,
        date=fr.session_data.date,
    )

    bias = BiasReasoner().judge(fr)
    levels = LevelReasoner().analyze(fr)
    scenarios = ScenarioReasoner().generate(fr, bias, levels)
    narrative = NarrativeReasoner().narrate(fr)

    log.info(
        "reasoning_done",
        bias=bias.bias,
        confidence=f"{bias.confidence:.2f}",
        levels=len(levels),
        scenarios=len(scenarios),
    )

    return ReasoningReport(
        bias=bias,
        levels=levels,
        scenarios=scenarios,
        narrative=narrative,
    )
