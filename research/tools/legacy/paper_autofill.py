from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class AutoFillRule:
    keywords: tuple[str, ...]
    hypothesis: str
    formula: str
    data_fields: tuple[str, ...]


@dataclass(frozen=True)
class AutoFillSpec:
    hypothesis: str
    formula: str
    data_fields: tuple[str, ...]


DEFAULT_HYPOTHESIS = (
    "Short-horizon microstructure imbalance can predict near-term mid-price direction "
    "after controlling for spread and depth."
)
DEFAULT_FORMULA = "alpha_t = zscore(depth_imbalance_ppm_t) - 0.5 * zscore(spread_scaled_t)"
DEFAULT_FEATURE_FIELDS = (
    "spread_scaled",
    "depth_imbalance_ppm",
    "l1_bid_qty",
    "l1_ask_qty",
    "mid_price_x2",
)

_RULES: tuple[AutoFillRule, ...] = (
    AutoFillRule(
        keywords=("order flow imbalance", "order-flow imbalance", "ofi"),
        hypothesis=(
            "Signed order-flow imbalance predicts short-horizon price pressure, especially when queue "
            "imbalance aligns with the OFI direction."
        ),
        formula="alpha_t = zscore(ofi_l1_ema8_t) * sign(depth_imbalance_ema8_ppm_t)",
        data_fields=(
            "ofi_l1_raw",
            "ofi_l1_cum",
            "ofi_l1_ema8",
            "depth_imbalance_ppm",
            "depth_imbalance_ema8_ppm",
            "l1_bid_qty",
            "l1_ask_qty",
            "spread_scaled",
            "spread_ema8_scaled",
            "mid_price_x2",
            "microprice_x2",
        ),
    ),
    AutoFillRule(
        keywords=("multi-level", "multilevel", "lob"),
        hypothesis=(
            "Order-flow signals from deeper LOB levels add predictive value versus top-of-book-only "
            "signals for the next tick return."
        ),
        formula="alpha_t = sum_k (w_k * ofi_k_t), where w_k = 1 / max(1, k)",
        data_fields=(
            "l1_bid_qty",
            "l1_ask_qty",
            "depth_imbalance_ppm",
            "depth_imbalance_ema8_ppm",
            "spread_scaled",
            "mid_price_x2",
            "microprice_x2",
        ),
    ),
    AutoFillRule(
        keywords=("queue imbalance", "queue"),
        hypothesis=(
            "Queue imbalance at the best levels provides a fast proxy for one-tick directional pressure "
            "when spread remains stable."
        ),
        formula="alpha_t = zscore(l1_imbalance_ppm_t) - 0.25 * zscore(spread_scaled_t)",
        data_fields=(
            "l1_bid_qty",
            "l1_ask_qty",
            "l1_imbalance_ppm",
            "spread_scaled",
            "spread_ema8_scaled",
            "mid_price_x2",
        ),
    ),
    AutoFillRule(
        keywords=("spread", "liquidity", "depth slope"),
        hypothesis=(
            "Microstructure stress from widening spread and asymmetric depth tends to precede "
            "adverse short-horizon price moves."
        ),
        formula="alpha_t = -zscore(spread_ema8_scaled_t) + zscore(depth_imbalance_ema8_ppm_t)",
        data_fields=(
            "spread_scaled",
            "spread_ema8_scaled",
            "depth_imbalance_ppm",
            "depth_imbalance_ema8_ppm",
            "mid_price_x2",
            "microprice_x2",
        ),
    ),
)

_CITATION_RULES: dict[str, AutoFillRule] = {
    # Forecasting High Frequency Order Flow Imbalance
    "2408.03594": AutoFillRule(
        keywords=(),
        hypothesis=(
            "Near-term OFI distribution estimated from self-exciting order arrival dynamics can improve "
            "short-horizon directional forecasts."
        ),
        formula="alpha_t = zscore(ofi_l1_ema8_t) * sign(depth_imbalance_ema8_ppm_t)",
        data_fields=(
            "ofi_l1_raw",
            "ofi_l1_cum",
            "ofi_l1_ema8",
            "depth_imbalance_ppm",
            "depth_imbalance_ema8_ppm",
            "l1_bid_qty",
            "l1_ask_qty",
            "spread_scaled",
            "spread_ema8_scaled",
            "microprice_x2",
            "mid_price_x2",
        ),
    ),
    # Multi-Level Order-Flow Imbalance in a Limit Order Book
    "1907.06230": AutoFillRule(
        keywords=(),
        hypothesis=(
            "Multi-level order-flow imbalance adds incremental predictive power over top-of-book-only imbalance "
            "for next-tick mid-price moves."
        ),
        formula="alpha_t = sum_k (w_k * ofi_k_t), where w_k = 1 / max(1, k)",
        data_fields=(
            "l1_bid_qty",
            "l1_ask_qty",
            "depth_imbalance_ppm",
            "depth_imbalance_ema8_ppm",
            "spread_scaled",
            "microprice_x2",
            "mid_price_x2",
        ),
    ),
    # Queue Imbalance as a One-Tick-Ahead Price Predictor in a Limit Order Book
    "1512.03492": AutoFillRule(
        keywords=(),
        hypothesis=(
            "Queue imbalance at best bid/ask predicts one-tick-ahead direction "
            "with stronger effect when spread is tight."
        ),
        formula="alpha_t = zscore(l1_imbalance_ppm_t) - 0.25 * zscore(spread_scaled_t)",
        data_fields=(
            "l1_bid_qty",
            "l1_ask_qty",
            "l1_imbalance_ppm",
            "spread_scaled",
            "spread_ema8_scaled",
            "mid_price_x2",
        ),
    ),
}


def available_feature_ids() -> tuple[str, ...]:
    try:
        from hft_platform.feature.registry import default_feature_registry

        return tuple(default_feature_registry().get_default().feature_ids)
    except Exception:
        return DEFAULT_FEATURE_FIELDS


def infer_spec_from_text(
    *chunks: str,
    arxiv_ids: Iterable[str] = (),
    preferred_hypothesis: str | None = None,
    preferred_formula: str | None = None,
    preferred_fields: Iterable[str] = (),
) -> AutoFillSpec:
    text = _normalize_text(" ".join(chunks))
    selected_fields: list[str] = [str(field) for field in preferred_fields]
    hypothesis = _sanitize_section_text(preferred_hypothesis) or DEFAULT_HYPOTHESIS
    formula = _sanitize_formula_text(preferred_formula) or DEFAULT_FORMULA

    for raw_arxiv_id in arxiv_ids:
        arxiv_key = _normalize_arxiv_id(raw_arxiv_id)
        rule = _CITATION_RULES.get(arxiv_key)
        if rule is None:
            continue
        if hypothesis == DEFAULT_HYPOTHESIS:
            hypothesis = rule.hypothesis
        if formula == DEFAULT_FORMULA:
            formula = rule.formula
        selected_fields.extend(rule.data_fields)

    for rule in _RULES:
        if not _contains_any(text, rule.keywords):
            continue
        if hypothesis == DEFAULT_HYPOTHESIS:
            hypothesis = rule.hypothesis
        if formula == DEFAULT_FORMULA:
            formula = rule.formula
        selected_fields.extend(rule.data_fields)

    if not selected_fields:
        selected_fields.extend(DEFAULT_FEATURE_FIELDS)

    allowed = set(available_feature_ids())
    filtered = tuple(field for field in _dedupe(selected_fields) if field in allowed)
    if not filtered:
        filtered = tuple(available_feature_ids()[:4])

    return AutoFillSpec(
        hypothesis=_normalize_sentence(hypothesis),
        formula=_normalize_formula(formula),
        data_fields=filtered,
    )


def infer_spec_from_paper_refs(
    paper_refs: Iterable[str],
    *,
    project_root: Path | None = None,
    index_path: Path | None = None,
    seed_text: Iterable[str] = (),
) -> AutoFillSpec:
    refs = [str(ref).strip() for ref in paper_refs if str(ref).strip()]
    if not refs and not seed_text:
        return infer_spec_from_text("")

    root = project_root or Path(__file__).resolve().parents[2]
    resolved_index_path = index_path or (root / "research" / "knowledge" / "paper_index.json")
    index = _load_index_file(resolved_index_path)
    text_chunks: list[str] = [str(chunk) for chunk in seed_text]
    arxiv_ids: list[str] = []
    preferred_hypothesis: str | None = None
    preferred_formula: str | None = None
    preferred_fields: list[str] = []

    for ref in refs:
        row = _resolve_row(index, ref)
        if row is None:
            continue
        title = str(row.get("title", "")).strip()
        tags = " ".join(str(tag).strip() for tag in row.get("tags", []) if str(tag).strip())
        if title:
            text_chunks.append(title)
        if tags:
            text_chunks.append(tags)

        arxiv_id = str(row.get("arxiv_id", "")).strip()
        if arxiv_id:
            arxiv_ids.append(arxiv_id)

        note_file = str(row.get("note_file", "")).strip()
        if not note_file:
            continue
        note_path = (root / note_file).resolve()
        if not note_path.exists():
            continue
        try:
            note_text = note_path.read_text(encoding="utf-8")
        except OSError:
            continue
        text_chunks.append(note_text)

        if preferred_hypothesis is None:
            preferred_hypothesis = _extract_markdown_line_item(note_text, ("hypothesis",))
        if preferred_formula is None:
            preferred_formula = _extract_markdown_line_item(note_text, ("candidate formula", "formula"))
        preferred_fields.extend(_extract_markdown_feature_fields(note_text))

    return infer_spec_from_text(
        *text_chunks,
        arxiv_ids=arxiv_ids,
        preferred_hypothesis=preferred_hypothesis,
        preferred_formula=preferred_formula,
        preferred_fields=preferred_fields,
    )


def suggest_alpha_id(title: str, *, max_len: int = 48) -> str:
    raw = re.sub(r"[^a-z0-9]+", "_", str(title or "").lower()).strip("_")
    if not raw:
        return "alpha_prototype"
    parts = [part for part in raw.split("_") if part and part not in _STOP_WORDS]
    cleaned = "_".join(parts) or raw
    return cleaned[:max_len].strip("_") or "alpha_prototype"


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _normalize_sentence(text: str) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "").strip())
    return normalized.rstrip(".") + "."


def _normalize_formula(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def _normalize_arxiv_id(value: str) -> str:
    text = str(value or "").strip()
    if "/abs/" in text:
        text = text.split("/abs/")[-1].strip()
    return re.sub(r"v\d+$", "", text)


def _contains_any(text: str, keywords: Iterable[str]) -> bool:
    for keyword in keywords:
        if keyword in text:
            return True
    return False


def _dedupe(items: Iterable[str]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = str(item).strip()
        if not key or key in seen:
            continue
        out.append(key)
        seen.add(key)
    return tuple(out)


def _sanitize_section_text(text: str | None) -> str | None:
    if text is None:
        return None
    cleaned = re.sub(r"\s+", " ", str(text).strip()).strip("- ").strip()
    if not cleaned or "todo" in cleaned.lower():
        return None
    return cleaned


def _sanitize_formula_text(text: str | None) -> str | None:
    if text is None:
        return None
    cleaned = re.sub(r"\s+", " ", str(text).strip()).strip("- ").strip()
    cleaned = cleaned.strip("`")
    if not cleaned or "todo" in cleaned.lower():
        return None
    return cleaned


def _load_index_file(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            out[str(key)] = value
    return out


def _resolve_row(index: dict[str, dict[str, Any]], ref_or_arxiv: str) -> dict[str, Any] | None:
    key = str(ref_or_arxiv).strip()
    row = index.get(key)
    if row is not None:
        return row
    normalized = _normalize_arxiv_id(key)
    for candidate in index.values():
        candidate_arxiv = _normalize_arxiv_id(str(candidate.get("arxiv_id", "")).strip())
        if candidate_arxiv and candidate_arxiv == normalized:
            return candidate
    return None


def _extract_markdown_line_item(markdown: str, headings: tuple[str, ...]) -> str | None:
    section = _extract_markdown_section(markdown, headings)
    if not section:
        return None
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            return stripped[2:].strip()
        return stripped
    return None


def _extract_markdown_feature_fields(markdown: str) -> tuple[str, ...]:
    section = _extract_markdown_section(markdown, ("relevant features", "data fields"))
    if not section:
        return ()
    fields: list[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("-"):
            continue
        for token in re.findall(r"`([^`]+)`", stripped):
            candidate = token.strip()
            if candidate:
                fields.append(candidate)
        if "`" not in stripped:
            candidate = stripped.lstrip("-").strip()
            if re.fullmatch(r"[a-zA-Z0-9_]+", candidate):
                fields.append(candidate)
    return _dedupe(fields)


def _extract_markdown_section(markdown: str, headings: tuple[str, ...]) -> str:
    lines = str(markdown or "").splitlines()
    target_idx: int | None = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("##"):
            continue
        heading = re.sub(r"#+", "", stripped).strip().lower()
        if any(h in heading for h in headings):
            target_idx = idx + 1
            break
    if target_idx is None:
        return ""
    chunk: list[str] = []
    for line in lines[target_idx:]:
        stripped = line.strip()
        if stripped.startswith("##"):
            break
        chunk.append(line)
    return "\n".join(chunk).strip()


_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "for",
        "from",
        "in",
        "is",
        "of",
        "on",
        "the",
        "to",
        "with",
    }
)
