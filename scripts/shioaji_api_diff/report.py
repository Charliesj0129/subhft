"""Emit machine diff JSON + the human Markdown runbook for a version pair."""

from __future__ import annotations

import json
from typing import Any

from . import classify
from .diff import diff_snapshots


def build_diff_doc(from_v: str, to_v: str, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Compute the classified, summarized diff document for one version pair."""
    classified = classify.classify_records(diff_snapshots(old, new))
    summary = classify.summarize(classified)
    return {
        "from": from_v,
        "to": to_v,
        "generated_by": "scripts/shioaji_api_diff",
        "from_sha256": old.get("snapshot_sha256"),
        "to_sha256": new.get("snapshot_sha256"),
        "verdict": summary["verdict"],
        "counts": summary["counts"],
        "changes": classified,
    }


def canonical_json(doc: dict[str, Any]) -> str:
    return json.dumps(doc, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


# --------------------------------------------------------------------------- #
# Markdown rendering.
# --------------------------------------------------------------------------- #
_VERDICT_BLURB = {
    "SAFE": "No platform-impacting breaking change detected — safe to adopt "
            "(review any behavioral/config notes below).",
    "NEEDS-SHIM": "Breaking changes exist but all are mechanically shimmable "
                  "(e.g. enum-member aliases). Apply the shims, then upgrade.",
    "BLOCKED": "Breaking changes require adapter work before the pin can move. "
               "Clear the remediation checklist first.",
}


def _table(rows: list[list[str]], header: list[str]) -> list[str]:
    out = ["| " + " | ".join(header) + " |",
           "| " + " | ".join("---" for _ in header) + " |"]
    for r in rows:
        out.append("| " + " | ".join(_md_cell(c) for c in r) + " |")
    return out


def _md_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _section(doc: dict[str, Any]) -> list[str]:
    changes = doc["changes"]
    out: list[str] = [f"## {doc['from']} → {doc['to']}", ""]
    c = doc["counts"]
    out.append(f"**Verdict: `{doc['verdict']}`** — {_VERDICT_BLURB.get(doc['verdict'], '')}")
    out.append("")
    out.append(f"- Breaking (platform): **{c['breaking_platform']}** · "
               f"Breaking (other): {c['breaking_other']} · Additive: {c['additive']} · "
               f"Behavioral: {c['behavioral']} · Benign: {c['benign']} · "
               f"Informational: {c['informational']}")
    out.append("")

    breaking = [r for r in changes if r["classification"] == classify.BREAKING and r["platform_used"]]
    out.append("### Breaking changes for the platform")
    if breaking:
        rows = [[r["qualname"], r["kind"], f"`{r['before']}` → `{r['after']}`",
                 r["remediation"], "shim" if r["shimmable"] else "code"] for r in breaking]
        out += _table(rows, ["Symbol", "Change", "Before → After", "Remediation", "Fix"])
    else:
        out.append("_None._")
    out.append("")

    additive = [r for r in changes if r["classification"] == classify.ADDITIVE]
    out.append("### Additive changes (safe to adopt)")
    out.append(_bullets(additive) if additive else "_None._")
    out.append("")

    behavioral = [r for r in changes if r["classification"] == classify.BEHAVIORAL]
    out.append("### Behavioral / config changes (review)")
    if behavioral:
        rows = [[r["qualname"], r["kind"], f"`{r['before']}` → `{r['after']}`", r["remediation"]]
                for r in behavioral]
        out += _table(rows, ["Symbol", "Change", "Before → After", "Note"])
    else:
        out.append("_None._")
    out.append("")

    opaque = [r for r in changes if r["section"] == "compiled"
              and r["classification"] in {classify.INFORMATIONAL, classify.BREAKING, classify.BEHAVIORAL}]
    out.append("### Opaque-layer signal (compiled .so)")
    out.append(_bullets(opaque) if opaque else "_No compiled-layer change detected._")
    out.append("")

    out.append("### Remediation checklist")
    if breaking:
        for r in breaking:
            out.append(f"- [ ] {r['remediation']} — handle `{r['kind']}` of `{r['qualname']}`")
    else:
        out.append("- [x] Nothing to remediate for the platform.")
    out.append("")
    return out


def _bullets(records: list[dict[str, Any]]) -> str:
    return "\n".join(f"- `{r['qualname']}` — {r['kind']}" for r in records)


def _verification_notes() -> list[str]:
    """Deep reverse-engineering verification against the 1.5.3 artifacts.

    The static surface diff is conservative: it flags every removed symbol for
    human review. This section records what that review found when the actual
    1.5.3 build was reverse-engineered — the compiled Rust `shioaji` binary plus
    an installed `shioaji[speed]==1.5.3` introspected WITHOUT login — so the
    go/no-go reads the verified reality, not just the surface flags.
    """
    return [
        "## Deep reverse-engineering verification (1.5.3 artifacts)",
        "",
        "Verified against the compiled Rust `shioaji` binary and an installed "
        "`shioaji[speed]==1.5.3` introspected without logging in. The table above "
        "is intentionally conservative; these are the resolved findings that drive "
        "the actual decision:",
        "",
        "- **`api.quote` is NOT removed** — it is a `property` on `Shioaji` "
        "returning a `_QuoteProxy` that still exposes `subscribe`, `unsubscribe`, "
        "all four `set_on_{tick,bidask}_{stk,fop}_v1_callback`, and "
        "`set_event_callback`. The adapter's `_quote_api()` path and the "
        "market-data flow are intact — the earlier 'silent market-data outage' "
        "scenario does not occur.",
        "- **Reconnect event codes are unchanged** — `set_event_callback` exists "
        "on the proxy and the vendor's own 1.5.3 TROUBLESHOOTING.md documents "
        "`event_code == 12` (Reconnecting) / `== 13` (Reconnected) with the same "
        "`(resp_code, event_code, info, event)` signature the adapter registers "
        "via `client.py:_register_event_callback`. Reconnect/resubscribe fires.",
        "- **Solace moved into the Rust core** — the *Python* "
        "`shioaji.backend.solace` module is gone (the arity shim "
        "ImportError-skips), but Solace itself is compiled in and self-manages "
        "reconnect (`FLOW_MAX_RECONNECT_TRIES`, `GD_RECONNECT_FAIL_ACTION_*`). The "
        "shim is now harmless dead code, not a reintroduced SIGABRT.",
        "- **All adapter-referenced enums resolve** via `sj.constant.*` "
        "deprecation shims (same objects: `sj.constant.QuoteType is sj.QuoteType`). "
        "`QuoteType.BidAsk` value `bidask`->`bid_ask` is benign (the adapter "
        "passes the enum object; `bidask` is still a wire alias). `StockOrder`/"
        "`FuturesOrder` construct with the adapter's kwargs.",
        "- **`FuturesOrderType` removed -> benign**: `order_gateway.py` uses "
        "`getattr(sdk.constant, 'FuturesOrderType', None)` and falls back to "
        "`OrderType`.",
        "- **`QuoteVersion.v0` removed -> benign in practice**: default config is "
        "`v1`; the explicit-v0 path is guarded by `_supports_quote_v0()` (False on "
        "1.5.3 — `_QuoteProxy` has no non-v1 setters), so "
        "`sj.constant.QuoteVersion.v0` is unreachable. Recommend dropping dead v0 "
        "support (1.5.3 is v1-only).",
        "",
        "### Residual — needs a live `HFT_MODE=sim` soak (cannot be cleared statically)",
        "",
        "- It is a full **Rust transport rewrite**: callback latency "
        "(place/cancel P50/P95/P99 for Gate D), GIL/threading behaviour, memory, "
        "and the new internal reconnect/backoff timing are unverified by surface "
        "introspection.",
        "- Tick/bidask **prices are now `Decimal`** in callbacks — confirm the "
        "scaled-int x10000 conversion path.",
        "- The adapter runs entirely on **deprecated** API paths "
        "(`sj.constant.*`, `api.quote.*`, possibly 2-arg callbacks emit "
        "DeprecationWarning). Functional now; owe a migration to the top-level "
        "API before a future release drops the shims.",
        "",
    ]


def render_markdown(docs: list[dict[str, Any]], generated_on: str | None = None) -> str:
    lines: list[str] = [
        "# Shioaji SDK Version-Diff Runbook",
        "",
        "_Generated by `scripts/shioaji_api_diff`. Do not hand-edit — rerun "
        "`make shioaji-diff`._",
    ]
    if generated_on:
        lines.append(f"_Snapshot date: {generated_on}._")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines += _table(
        [[f"{d['from']} → {d['to']}", f"`{d['verdict']}`",
          str(d["counts"]["breaking_platform"]), str(d["counts"]["additive"]),
          str(d["counts"]["behavioral"])] for d in docs],
        ["Pair", "Verdict", "Breaking (platform)", "Additive", "Behavioral"],
    )
    lines.append("")
    lines += _verification_notes()
    for doc in docs:
        lines += _section(doc)
    lines.append("## Appendix: regenerate")
    lines.append("")
    lines.append("```")
    lines.append("make shioaji-surface   # (re)capture per-version surfaces in throwaway venvs")
    lines.append("make shioaji-diff      # rebuild machine diffs + this runbook")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)
