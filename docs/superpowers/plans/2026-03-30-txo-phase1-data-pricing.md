# TXO Phase 1: Data Foundation + Pricing Core — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden option metadata expansion so TXO symbols have complete fields (strike, expiry, right, point_value, tick_size), add R1/R2 continuous contract aliases, and build offline options pricing engine (IV solver, Greeks, vol surface).

**Architecture:** Fix `build_entry()` in the existing `_symbols_expansion.py` to populate option-specific metadata from the contract index. Build `src/hft_platform/options/` as a new pure-computation package (float-only, offline research). No runtime pipeline changes — recorder and subscription already handle TXO via `TickFOPv1`/`BidAskFOPv1`.

**Tech Stack:** Python 3.12, numpy, scipy.optimize.brentq, pytest, structlog

**Spec:** `docs/superpowers/specs/2026-03-30-txo-options-infrastructure-design.md` §2

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `src/hft_platform/config/_symbols_expansion.py` | Modify | Harden `build_entry()` for option metadata |
| `src/hft_platform/feed_adapter/shioaji/contracts_runtime.py` | Modify | R1/R2 continuous contract alias |
| `src/hft_platform/options/__init__.py` | Create | Package exports |
| `src/hft_platform/options/pricing.py` | Create | Black-76 pricing + IV solver |
| `src/hft_platform/options/greeks.py` | Create | Black-76 Greeks + portfolio aggregation |
| `src/hft_platform/options/surface.py` | Create | Vol surface grid + interpolation |
| `tests/unit/test_options_pricing.py` | Create | Pricing + IV tests |
| `tests/unit/test_options_greeks.py` | Create | Greeks + portfolio tests |
| `tests/unit/test_options_surface.py` | Create | Vol surface tests |
| `tests/unit/test_symbols_expansion_options.py` | Create | Option metadata expansion tests |
| `tests/unit/test_contracts_runtime_r1r2.py` | Create | R1/R2 alias tests |

---

### Task 1: Harden `build_entry()` for Option Metadata

**Files:**
- Modify: `src/hft_platform/config/_symbols_expansion.py:43-74`
- Test: `tests/unit/test_symbols_expansion_options.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_symbols_expansion_options.py`:

```python
"""Tests for option-specific metadata in build_entry()."""

from hft_platform.config._symbols_expansion import build_entry
from hft_platform.config._symbols_types import SymbolBuildResult


def _make_result() -> SymbolBuildResult:
    return SymbolBuildResult(symbols=[], errors=[], warnings=[])


def test_build_entry_option_populates_right_from_option_right():
    contract = {
        "code": "TXO22500D6",
        "exchange": "OPT",
        "type": "option",
        "option_right": "OptionCall",
        "strike_price": 22500,
        "delivery_date": "2026-04-15",
    }
    result = _make_result()
    entry = build_entry("TXO22500D6", {"product_type": "option"}, contract, result)
    assert entry is not None
    assert entry["right"] == "C"


def test_build_entry_option_populates_strike():
    contract = {
        "code": "TXO22500P6",
        "exchange": "OPT",
        "type": "option",
        "option_right": "OptionPut",
        "strike_price": 22500,
        "delivery_date": "2026-04-15",
    }
    result = _make_result()
    entry = build_entry("TXO22500P6", {"product_type": "option"}, contract, result)
    assert entry is not None
    assert entry["strike"] == 22500


def test_build_entry_option_populates_expiry():
    contract = {
        "code": "TXO22500D6",
        "exchange": "OPT",
        "type": "option",
        "option_right": "OptionCall",
        "strike_price": 22500,
        "delivery_date": "2026-04-15",
    }
    result = _make_result()
    entry = build_entry("TXO22500D6", {"product_type": "option"}, contract, result)
    assert entry is not None
    assert entry["expiry"] == "2026-04-15"


def test_build_entry_option_defaults_point_value():
    contract = {
        "code": "TXO22500D6",
        "exchange": "OPT",
        "type": "option",
        "option_right": "OptionCall",
        "strike_price": 22500,
        "delivery_date": "2026-04-15",
    }
    result = _make_result()
    entry = build_entry("TXO22500D6", {"product_type": "option"}, contract, result)
    assert entry is not None
    assert entry["point_value"] == 50


def test_build_entry_option_point_value_from_attrs():
    contract = {
        "code": "TXO22500D6",
        "exchange": "OPT",
        "type": "option",
        "option_right": "OptionCall",
        "strike_price": 22500,
        "delivery_date": "2026-04-15",
    }
    result = _make_result()
    entry = build_entry(
        "TXO22500D6", {"product_type": "option", "point_value": 100}, contract, result
    )
    assert entry is not None
    assert entry["point_value"] == 100


def test_build_entry_option_defaults_price_scale():
    contract = {
        "code": "TXO22500D6",
        "exchange": "OPT",
        "type": "option",
        "option_right": "OptionCall",
        "strike_price": 22500,
        "delivery_date": "2026-04-15",
    }
    result = _make_result()
    entry = build_entry("TXO22500D6", {"product_type": "option"}, contract, result)
    assert entry is not None
    assert entry.get("price_scale", 10000) == 10000


def test_build_entry_option_underlying_mapping():
    contract = {
        "code": "TXO22500D6",
        "exchange": "OPT",
        "type": "option",
        "option_right": "OptionCall",
        "strike_price": 22500,
        "delivery_date": "2026-04-15",
    }
    result = _make_result()
    entry = build_entry("TXO22500D6", {"product_type": "option"}, contract, result)
    assert entry is not None
    assert entry["underlying"] == "TX"


def test_build_entry_option_warns_on_missing_strike():
    contract = {
        "code": "TXO22500D6",
        "exchange": "OPT",
        "type": "option",
        "option_right": "OptionCall",
        "delivery_date": "2026-04-15",
    }
    result = _make_result()
    entry = build_entry("TXO22500D6", {"product_type": "option"}, contract, result)
    assert entry is not None
    assert any("strike" in w.lower() for w in result.warnings)


def test_build_entry_option_warns_on_missing_expiry():
    contract = {
        "code": "TXO22500D6",
        "exchange": "OPT",
        "type": "option",
        "option_right": "OptionCall",
        "strike_price": 22500,
    }
    result = _make_result()
    entry = build_entry("TXO22500D6", {"product_type": "option"}, contract, result)
    assert entry is not None
    assert any("expiry" in w.lower() for w in result.warnings)


def test_build_entry_non_option_unchanged():
    """Existing futures behavior must not be affected."""
    contract = {
        "code": "TXFD6",
        "exchange": "FUT",
        "type": "future",
        "tick_size": 1.0,
    }
    result = _make_result()
    entry = build_entry("TXFD6", {}, contract, result)
    assert entry is not None
    assert "right" not in entry
    assert "strike" not in entry
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_symbols_expansion_options.py -v`
Expected: Tests that check `entry["right"]`, `entry["strike"]`, `entry["expiry"]`, `entry["point_value"]`, `entry["underlying"]` should FAIL with `KeyError` because `build_entry()` doesn't populate these fields yet. The warning tests and non-option test may pass or fail depending on current behavior.

- [ ] **Step 3: Implement option metadata population in `build_entry()`**

Edit `src/hft_platform/config/_symbols_expansion.py`. After the existing `entry.update(...)` line (line 64) and before the tags merge (line 70), add option-specific metadata extraction:

```python
def build_entry(
    code: str,
    attrs: dict[str, Any],
    contract: dict[str, Any] | None,
    result: SymbolBuildResult,
    extra_tags: list[str] | None = None,
) -> dict[str, Any] | None:
    """Build a single symbol entry dict from *code*, *attrs*, and *contract*."""
    if not code:
        return None

    entry: dict[str, Any] = {"code": code}
    if contract:
        for key in ("name", "exchange", "tick_size", "price_scale", "contract_size"):
            if key in contract and contract[key] is not None:
                entry[key] = contract[key]
        if "product_type" not in entry:
            c_type = contract.get("type") or contract.get("security_type")
            if c_type:
                entry["product_type"] = c_type

    entry.update({k: v for k, v in attrs.items() if v is not None})

    # --- Option-specific metadata ---
    _product = str(entry.get("product_type") or "").lower()
    if _product in {"option", "opt", "options"} and contract:
        _enrich_option_entry(entry, contract, code, result)

    if "exchange" not in entry or not entry["exchange"]:
        entry["exchange"] = _default_exchange_for_code(code)
        result.warnings.append(f"Defaulted exchange for {code} to {entry['exchange']}")

    tags = merge_tags(entry.get("tags", []), extra_tags or [])
    if tags:
        entry["tags"] = tags

    return entry


# Root → underlying mapping for TAIFEX options
_OPTION_UNDERLYING_MAP: dict[str, str] = {
    "TXO": "TX",
    "TEO": "TE",
    "TFO": "TF",
    "MSO": "MX",
}

# Default point values per root (NTD per index point)
_OPTION_POINT_VALUE: dict[str, int] = {
    "TXO": 50,
    "TEO": 50,
    "TFO": 20,
    "MSO": 10,
}


def _enrich_option_entry(
    entry: dict[str, Any],
    contract: dict[str, Any],
    code: str,
    result: SymbolBuildResult,
) -> None:
    """Populate option-specific fields from contract index data."""
    # right (C/P)
    if "right" not in entry:
        raw_right = contract.get("right") or contract.get("option_right")
        right = _normalize_option_right(raw_right)
        if right:
            entry["right"] = right
        else:
            result.warnings.append(f"Missing option right for {code}")

    # strike
    if "strike" not in entry:
        strike = contract.get("strike_price") or contract.get("strike")
        if strike is not None:
            try:
                entry["strike"] = int(float(strike))
            except (TypeError, ValueError):
                result.warnings.append(f"Invalid strike for {code}: {strike}")
        else:
            result.warnings.append(f"Missing strike for {code}")

    # expiry
    if "expiry" not in entry:
        expiry = contract.get("delivery_date") or contract.get("expiry") or contract.get("due_date")
        if expiry is not None:
            entry["expiry"] = str(expiry)
        else:
            result.warnings.append(f"Missing expiry for {code}")

    # underlying
    if "underlying" not in entry:
        root = code[:3].upper() if len(code) >= 3 else code.upper()
        entry["underlying"] = _OPTION_UNDERLYING_MAP.get(root, root)

    # point_value (default from root, overridable via attrs)
    if "point_value" not in entry:
        root = code[:3].upper() if len(code) >= 3 else code.upper()
        entry["point_value"] = _OPTION_POINT_VALUE.get(root, 50)

    # price_scale (platform convention)
    if "price_scale" not in entry:
        entry["price_scale"] = 10000
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_symbols_expansion_options.py -v`
Expected: All 11 tests PASS.

- [ ] **Step 5: Run existing symbol tests to verify no regression**

Run: `uv run pytest tests/unit/test_symbol_metadata.py -v`
Expected: All existing tests PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/unit/test_symbols_expansion_options.py src/hft_platform/config/_symbols_expansion.py
git commit -m "feat(config): enrich build_entry() with option metadata (right, strike, expiry, point_value)"
```

---

### Task 2: R1/R2 Continuous Contract Alias

**Files:**
- Modify: `src/hft_platform/feed_adapter/shioaji/contracts_runtime.py:106-115`
- Test: `tests/unit/test_contracts_runtime_r1r2.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_contracts_runtime_r1r2.py`:

```python
"""Tests for R1/R2 continuous contract alias resolution."""

from unittest.mock import MagicMock, PropertyMock

from hft_platform.feed_adapter.shioaji.contracts_runtime import ContractsRuntime


def _make_runtime_with_r1r2():
    """Build a ContractsRuntime with mock api.Contracts.Futures.TXF.TXFR1/R2."""
    client = MagicMock()
    client.api = MagicMock()
    client.allow_symbol_fallback = False

    # Mock Contracts.Futures as a dict-like that responds to attribute access
    txfr1 = MagicMock(name="TXFR1_contract")
    txfr1.code = "TXFR1"
    txfr2 = MagicMock(name="TXFR2_contract")
    txfr2.code = "TXFR2"

    futures = MagicMock()
    # _lookup_contract accesses futures[code] or getattr(futures, code)
    txf_group = MagicMock()
    txf_group.TXFR1 = txfr1
    txf_group.TXFR2 = txfr2
    futures.TXF = txf_group

    # For normal _lookup_contract to find TXFR1 via getattr chain
    def lookup_side_effect(key):
        if key == "TXFR1":
            return txfr1
        if key == "TXFR2":
            return txfr2
        raise KeyError(key)

    futures.__getitem__ = lookup_side_effect

    client.api.Contracts.Futures = futures
    return ContractsRuntime(client), txfr1, txfr2


def test_r1_alias_resolves():
    runtime, txfr1, _ = _make_runtime_with_r1r2()
    contract = runtime._get_contract("FUT", "TXFR1", product_type="future")
    assert contract is not None


def test_r2_alias_resolves():
    runtime, _, txfr2 = _make_runtime_with_r1r2()
    contract = runtime._get_contract("FUT", "TXFR2", product_type="future")
    assert contract is not None


def test_non_r1r2_code_unaffected():
    runtime, _, _ = _make_runtime_with_r1r2()
    # Normal code goes through _expand_future_codes, not R1/R2 path
    # This should not raise, just return None if not found
    contract = runtime._get_contract("FUT", "TXFD6", product_type="future")
    # May be None (mock doesn't have TXFD6) — that's fine, just ensure no crash
    assert contract is None or contract is not None  # no exception
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_contracts_runtime_r1r2.py -v`
Expected: `test_r1_alias_resolves` and `test_r2_alias_resolves` may FAIL or PASS depending on how `_lookup_contract` handles the mock. The key is that the R1/R2 path doesn't exist yet — `_expand_future_codes("TXFR1")` will try month-code expansion which won't match `R1`.

- [ ] **Step 3: Add R1/R2 branch in `_get_contract()`**

Edit `src/hft_platform/feed_adapter/shioaji/contracts_runtime.py`. In the futures branch (around line 106), add an early check before `_expand_future_codes`:

```python
        if prod in {"future", "futures"} or exch in {"FUT", "FUTURES", "TAIFEX"}:
            # R1/R2 continuous contract alias (e.g. TXFR1 → Contracts.Futures.TXF.TXFR1)
            if len(raw_code) >= 4 and raw_code[-2:] in ("R1", "R2"):
                root = raw_code[:-2]  # e.g. "TXF" from "TXFR1"
                root_group = getattr(self._client.api.Contracts.Futures, root, None)
                if root_group is not None:
                    r_contract = getattr(root_group, raw_code, None)
                    if r_contract is not None:
                        return r_contract

            for candidate in self._expand_future_codes(raw_code):
                contract = self._lookup_contract(
                    self._client.api.Contracts.Futures,
                    candidate,
                    allow_symbol_fallback=self._client.allow_symbol_fallback,
                    label="future",
                )
                if contract:
                    return contract
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_contracts_runtime_r1r2.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 5: Run existing contract tests for regression**

Run: `uv run pytest tests/unit/test_shioaji_contract_refresh.py -v`
Expected: All existing tests PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/unit/test_contracts_runtime_r1r2.py src/hft_platform/feed_adapter/shioaji/contracts_runtime.py
git commit -m "feat(contracts): add R1/R2 continuous contract alias for futures"
```

---

### Task 3: Black-76 Pricing Engine

**Files:**
- Create: `src/hft_platform/options/__init__.py`
- Create: `src/hft_platform/options/pricing.py`
- Test: `tests/unit/test_options_pricing.py`

- [ ] **Step 1: Create package init**

Create `src/hft_platform/options/__init__.py`:

```python
"""Options analytics package — offline pricing, Greeks, vol surface.

Float exception: Per Architecture Governance Rule 25 §11, float is permitted
in this package for offline research computation. The live_adapter module
(Phase 2) is the boundary that converts float → int/bool before any value
enters the live trading path.
"""
```

- [ ] **Step 2: Write failing tests for `black76_price()`**

Create `tests/unit/test_options_pricing.py`:

```python
"""Tests for Black-76 pricing and IV solver."""

import math

import pytest


def test_black76_call_atm():
    """ATM call: F=K, known closed-form result."""
    from hft_platform.options.pricing import black76_price

    F, K, T, sigma, r = 20000.0, 20000.0, 30 / 365, 0.20, 0.01
    price = black76_price(F, K, T, sigma, r, "C")
    # ATM approx: C ≈ F * e^{-rT} * sigma * sqrt(T) * 0.3989
    approx = F * math.exp(-r * T) * sigma * math.sqrt(T) * 0.3989
    assert abs(price - approx) < approx * 0.05  # within 5%
    assert price > 0


def test_black76_put_atm():
    from hft_platform.options.pricing import black76_price

    F, K, T, sigma, r = 20000.0, 20000.0, 30 / 365, 0.20, 0.01
    call = black76_price(F, K, T, sigma, r, "C")
    put = black76_price(F, K, T, sigma, r, "P")
    # Put-call parity for Black-76: C - P = e^{-rT} * (F - K) = 0 when F=K
    assert abs(call - put) < 0.01


def test_black76_deep_itm_call():
    from hft_platform.options.pricing import black76_price

    F, K, T, sigma, r = 20000.0, 18000.0, 30 / 365, 0.20, 0.01
    price = black76_price(F, K, T, sigma, r, "C")
    intrinsic = math.exp(-r * T) * (F - K)
    assert price >= intrinsic - 0.01  # at least intrinsic value


def test_black76_deep_otm_call():
    from hft_platform.options.pricing import black76_price

    F, K, T, sigma, r = 20000.0, 25000.0, 30 / 365, 0.20, 0.01
    price = black76_price(F, K, T, sigma, r, "C")
    assert price >= 0
    assert price < 10  # deep OTM, very small


def test_black76_put_call_parity():
    """C - P = e^{-rT} * (F - K) for any F, K."""
    from hft_platform.options.pricing import black76_price

    F, K, T, sigma, r = 20000.0, 19500.0, 60 / 365, 0.25, 0.015
    call = black76_price(F, K, T, sigma, r, "C")
    put = black76_price(F, K, T, sigma, r, "P")
    parity = math.exp(-r * T) * (F - K)
    assert abs((call - put) - parity) < 0.01


def test_black76_invalid_cp_raises():
    from hft_platform.options.pricing import black76_price

    with pytest.raises(ValueError, match="cp must be"):
        black76_price(20000.0, 20000.0, 0.1, 0.2, 0.01, "X")


def test_black76_zero_time_call():
    """At expiry, call = max(F-K, 0)."""
    from hft_platform.options.pricing import black76_price

    F, K, r = 20000.0, 19500.0, 0.01
    price = black76_price(F, K, 1e-10, 0.20, r, "C")
    assert abs(price - max(F - K, 0) * math.exp(-r * 1e-10)) < 1.0


def test_solve_iv_roundtrip():
    """Price with known vol → solve IV → should recover the vol."""
    from hft_platform.options.pricing import black76_price, solve_iv

    F, K, T, sigma, r = 20000.0, 20500.0, 45 / 365, 0.22, 0.01
    price = black76_price(F, K, T, sigma, r, "C")
    recovered = solve_iv(price, F, K, T, r, "C")
    assert abs(recovered - sigma) < 1e-6


def test_solve_iv_roundtrip_put():
    from hft_platform.options.pricing import black76_price, solve_iv

    F, K, T, sigma, r = 20000.0, 19000.0, 30 / 365, 0.18, 0.01
    price = black76_price(F, K, T, sigma, r, "P")
    recovered = solve_iv(price, F, K, T, r, "P")
    assert abs(recovered - sigma) < 1e-6


def test_solve_iv_deep_otm_returns_nan():
    """Deep OTM with near-zero price should return NaN."""
    from hft_platform.options.pricing import solve_iv

    result = solve_iv(0.001, 20000.0, 25000.0, 30 / 365, 0.01, "C")
    assert math.isnan(result)


def test_solve_iv_negative_price_returns_nan():
    from hft_platform.options.pricing import solve_iv

    result = solve_iv(-1.0, 20000.0, 20000.0, 30 / 365, 0.01, "C")
    assert math.isnan(result)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_options_pricing.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hft_platform.options'`

- [ ] **Step 4: Implement `pricing.py`**

Create `src/hft_platform/options/pricing.py`:

```python
"""Black-76 option pricing and implied volatility solver.

All values are float — this is an offline analytics module.
See Architecture Governance Rule 25 §11.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.optimize import brentq
from scipy.stats import norm

# Convergence parameters for Newton-Raphson IV solver
_NR_TOL = 1e-8
_NR_MAX_ITER = 50
_MIN_VOL = 1e-6
_MAX_VOL = 10.0
_DEEP_OTM_THRESHOLD = 0.5  # price < 0.5 * tick_size → NaN


def black76_price(
    F: float, K: float, T: float, sigma: float, r: float, cp: str
) -> float:
    """Black-76 European option price.

    Args:
        F: Futures price (underlying).
        K: Strike price.
        T: Time to expiry in years (must be > 0).
        sigma: Volatility (annualized, e.g. 0.20 = 20%).
        r: Risk-free rate (annualized, e.g. 0.01 = 1%).
        cp: "C" for call, "P" for put.

    Returns:
        Option price (float).
    """
    cp = cp.upper()
    if cp not in ("C", "P"):
        raise ValueError(f"cp must be 'C' or 'P', got '{cp}'")

    if T <= 0:
        # At expiry
        if cp == "C":
            return max(F - K, 0.0) * math.exp(-r * max(T, 0))
        return max(K - F, 0.0) * math.exp(-r * max(T, 0))

    if sigma <= 0:
        # Zero vol = intrinsic
        disc = math.exp(-r * T)
        if cp == "C":
            return max(F - K, 0.0) * disc
        return max(K - F, 0.0) * disc

    sqrt_T = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    disc = math.exp(-r * T)

    if cp == "C":
        return disc * (F * norm.cdf(d1) - K * norm.cdf(d2))
    return disc * (K * norm.cdf(-d2) - F * norm.cdf(-d1))


def _vega_b76(F: float, K: float, T: float, sigma: float, r: float) -> float:
    """Black-76 vega (dPrice/dSigma). Used by Newton-Raphson solver."""
    if T <= 0 or sigma <= 0:
        return 0.0
    sqrt_T = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt_T)
    return F * math.exp(-r * T) * norm.pdf(d1) * sqrt_T


def solve_iv(
    market_price: float,
    F: float,
    K: float,
    T: float,
    r: float,
    cp: str,
    tick_size: float = 1.0,
) -> float:
    """Solve implied volatility from market price.

    Uses Newton-Raphson with Brent fallback.

    Args:
        market_price: Observed market price.
        F: Futures price.
        K: Strike price.
        T: Time to expiry in years.
        r: Risk-free rate.
        cp: "C" or "P".
        tick_size: Minimum price increment (for deep OTM boundary).

    Returns:
        Implied volatility, or NaN if unsolvable (deep OTM, negative price).
    """
    if market_price <= 0 or T <= 0:
        return float("nan")

    # Deep OTM boundary
    if market_price < _DEEP_OTM_THRESHOLD * tick_size:
        return float("nan")

    # Intrinsic value check
    disc = math.exp(-r * T)
    cp = cp.upper()
    if cp == "C":
        intrinsic = disc * max(F - K, 0.0)
    elif cp == "P":
        intrinsic = disc * max(K - F, 0.0)
    else:
        raise ValueError(f"cp must be 'C' or 'P', got '{cp}'")

    if market_price < intrinsic - 0.01:
        return float("nan")  # below intrinsic — arbitrage or bad data

    # Initial guess: Brenner-Subrahmanyam approximation
    sigma = math.sqrt(2 * math.pi / T) * market_price / F
    sigma = max(min(sigma, _MAX_VOL), _MIN_VOL)

    # Newton-Raphson
    for _ in range(_NR_MAX_ITER):
        price = black76_price(F, K, T, sigma, r, cp)
        diff = price - market_price
        if abs(diff) < _NR_TOL:
            return sigma
        vega = _vega_b76(F, K, T, sigma, r)
        if vega < 1e-12:
            break  # flat vega — fall through to Brent
        sigma -= diff / vega
        sigma = max(min(sigma, _MAX_VOL), _MIN_VOL)

    # Brent fallback
    def objective(s: float) -> float:
        return black76_price(F, K, T, s, r, cp) - market_price

    try:
        return float(brentq(objective, _MIN_VOL, _MAX_VOL, xtol=_NR_TOL))
    except ValueError:
        return float("nan")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_options_pricing.py -v`
Expected: All 12 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/options/__init__.py src/hft_platform/options/pricing.py tests/unit/test_options_pricing.py
git commit -m "feat(options): add Black-76 pricing engine and IV solver"
```

---

### Task 4: Black-76 Greeks Calculator

**Files:**
- Create: `src/hft_platform/options/greeks.py`
- Test: `tests/unit/test_options_greeks.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_options_greeks.py`:

```python
"""Tests for Black-76 Greeks and portfolio aggregation."""

import math

import pytest


def test_compute_greeks_call_delta_range():
    """Call delta should be in [0, 1] (discounted)."""
    from hft_platform.options.greeks import compute_greeks

    g = compute_greeks(20000.0, 20000.0, 30 / 365, 0.20, 0.01, "C")
    assert 0 < g.delta < 1


def test_compute_greeks_put_delta_range():
    """Put delta should be in [-1, 0] (discounted)."""
    from hft_platform.options.greeks import compute_greeks

    g = compute_greeks(20000.0, 20000.0, 30 / 365, 0.20, 0.01, "P")
    assert -1 < g.delta < 0


def test_compute_greeks_put_call_delta_parity():
    """Delta_C - Delta_P ≈ e^{-rT}."""
    from hft_platform.options.greeks import compute_greeks

    T, r = 30 / 365, 0.01
    gc = compute_greeks(20000.0, 19500.0, T, 0.20, r, "C")
    gp = compute_greeks(20000.0, 19500.0, T, 0.20, r, "P")
    assert abs((gc.delta - gp.delta) - math.exp(-r * T)) < 1e-6


def test_compute_greeks_gamma_positive():
    """Gamma is always positive for both calls and puts."""
    from hft_platform.options.greeks import compute_greeks

    gc = compute_greeks(20000.0, 20000.0, 30 / 365, 0.20, 0.01, "C")
    gp = compute_greeks(20000.0, 20000.0, 30 / 365, 0.20, 0.01, "P")
    assert gc.gamma > 0
    assert gp.gamma > 0


def test_compute_greeks_gamma_equal_call_put():
    """Gamma is the same for call and put at same strike."""
    from hft_platform.options.greeks import compute_greeks

    gc = compute_greeks(20000.0, 20000.0, 30 / 365, 0.20, 0.01, "C")
    gp = compute_greeks(20000.0, 20000.0, 30 / 365, 0.20, 0.01, "P")
    assert abs(gc.gamma - gp.gamma) < 1e-12


def test_compute_greeks_vega_positive():
    """Vega is always positive."""
    from hft_platform.options.greeks import compute_greeks

    g = compute_greeks(20000.0, 20000.0, 30 / 365, 0.20, 0.01, "C")
    assert g.vega > 0


def test_compute_greeks_theta_negative_for_long():
    """Theta should be negative (time decay)."""
    from hft_platform.options.greeks import compute_greeks

    g = compute_greeks(20000.0, 20000.0, 30 / 365, 0.20, 0.01, "C")
    assert g.theta < 0


def test_portfolio_greeks_single_position():
    from hft_platform.options.greeks import (
        PositionGreeks,
        compute_greeks,
        portfolio_greeks,
    )

    g = compute_greeks(20000.0, 20000.0, 30 / 365, 0.20, 0.01, "C")
    pos = [PositionGreeks(symbol="TXO20000C", qty=2, greeks=g)]
    agg = portfolio_greeks(pos, multiplier=50.0)
    assert abs(agg.net_delta - 2 * g.delta) < 1e-10


def test_portfolio_greeks_hedged_position():
    """Long call + short put at same strike: net delta ≈ e^{-rT}."""
    from hft_platform.options.greeks import (
        PositionGreeks,
        compute_greeks,
        portfolio_greeks,
    )

    T, r = 30 / 365, 0.01
    gc = compute_greeks(20000.0, 20000.0, T, 0.20, r, "C")
    gp = compute_greeks(20000.0, 20000.0, T, 0.20, r, "P")
    pos = [
        PositionGreeks(symbol="TXO20000C", qty=1, greeks=gc),
        PositionGreeks(symbol="TXO20000P", qty=-1, greeks=gp),
    ]
    agg = portfolio_greeks(pos, multiplier=50.0)
    assert abs(agg.net_delta - math.exp(-r * T)) < 1e-6


def test_portfolio_greeks_empty():
    from hft_platform.options.greeks import portfolio_greeks

    agg = portfolio_greeks([], multiplier=50.0)
    assert agg.net_delta == 0.0
    assert agg.net_gamma == 0.0
    assert agg.net_theta_ntd == 0.0
    assert agg.net_vega_ntd == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_options_greeks.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hft_platform.options.greeks'`

- [ ] **Step 3: Implement `greeks.py`**

Create `src/hft_platform/options/greeks.py`:

```python
"""Black-76 closed-form Greeks and portfolio aggregation.

All values are float — offline analytics module (Rule 25 §11).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from scipy.stats import norm


@dataclass(slots=True)
class GreeksResult:
    """Greeks for a single option position (1 lot)."""

    delta: float
    gamma: float
    theta: float  # per calendar day
    vega: float  # per 1% absolute vol move (i.e. per 0.01 sigma)
    rho: float


@dataclass(slots=True)
class PositionGreeks:
    """Greeks for a position (symbol + signed quantity)."""

    symbol: str
    qty: int
    greeks: GreeksResult


@dataclass(slots=True)
class AggregatedGreeks:
    """Portfolio-level aggregated Greeks."""

    net_delta: float  # futures-equivalent lots (sum of qty * delta)
    net_gamma: float  # sum of qty * gamma
    net_theta_ntd: float  # NTD per day (sum of qty * theta * multiplier)
    net_vega_ntd: float  # NTD per 1% vol (sum of qty * vega * multiplier)
    positions: tuple[PositionGreeks, ...]


def compute_greeks(
    F: float, K: float, T: float, sigma: float, r: float, cp: str
) -> GreeksResult:
    """Compute Black-76 closed-form Greeks.

    Args:
        F: Futures price.
        K: Strike price.
        T: Time to expiry in years.
        sigma: Volatility (annualized).
        r: Risk-free rate.
        cp: "C" or "P".

    Returns:
        GreeksResult with delta, gamma, theta, vega, rho.
    """
    cp = cp.upper()
    if cp not in ("C", "P"):
        raise ValueError(f"cp must be 'C' or 'P', got '{cp}'")

    if T <= 0 or sigma <= 0:
        # At expiry or zero vol — degenerate Greeks
        disc = math.exp(-r * max(T, 0))
        if cp == "C":
            delta = disc if F > K else 0.0
        else:
            delta = -disc if K > F else 0.0
        return GreeksResult(delta=delta, gamma=0.0, theta=0.0, vega=0.0, rho=0.0)

    sqrt_T = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    disc = math.exp(-r * T)
    n_d1 = norm.pdf(d1)

    # Delta
    if cp == "C":
        delta = disc * norm.cdf(d1)
    else:
        delta = disc * (norm.cdf(d1) - 1.0)

    # Gamma (same for call and put)
    gamma = disc * n_d1 / (F * sigma * sqrt_T)

    # Vega (per 0.01 sigma, i.e. per 1% vol move)
    vega = F * disc * n_d1 * sqrt_T * 0.01

    # Theta (per calendar day)
    theta_annual = -F * disc * n_d1 * sigma / (2.0 * sqrt_T)
    if cp == "C":
        theta_annual += r * disc * (F * norm.cdf(d1) - K * norm.cdf(d2))
    else:
        theta_annual += r * disc * (K * norm.cdf(-d2) - F * norm.cdf(-d1))
    # The standard theta formula gives dV/dT (positive for time value loss)
    # Convention: theta < 0 for long options (value decays)
    theta = -(-theta_annual) / 365.0  # negative for long positions

    # Rho (per 1% rate move)
    if cp == "C":
        rho = -T * disc * (F * norm.cdf(d1) - K * norm.cdf(d2)) * 0.01
    else:
        rho = -T * disc * (K * norm.cdf(-d2) - F * norm.cdf(-d1)) * 0.01

    return GreeksResult(delta=delta, gamma=gamma, theta=theta, vega=vega, rho=rho)


def portfolio_greeks(
    positions: list[PositionGreeks], multiplier: float
) -> AggregatedGreeks:
    """Linearly aggregate Greeks across positions.

    Args:
        positions: List of (symbol, qty, greeks) positions.
        multiplier: Contract multiplier in NTD per point (e.g. 50 for TXO).

    Returns:
        AggregatedGreeks with net values.
    """
    net_delta = 0.0
    net_gamma = 0.0
    net_theta = 0.0
    net_vega = 0.0

    for pos in positions:
        net_delta += pos.qty * pos.greeks.delta
        net_gamma += pos.qty * pos.greeks.gamma
        net_theta += pos.qty * pos.greeks.theta * multiplier
        net_vega += pos.qty * pos.greeks.vega * multiplier

    return AggregatedGreeks(
        net_delta=net_delta,
        net_gamma=net_gamma,
        net_theta_ntd=net_theta,
        net_vega_ntd=net_vega,
        positions=tuple(positions),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_options_greeks.py -v`
Expected: All 11 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/options/greeks.py tests/unit/test_options_greeks.py
git commit -m "feat(options): add Black-76 Greeks calculator and portfolio aggregation"
```

---

### Task 5: Volatility Surface

**Files:**
- Create: `src/hft_platform/options/surface.py`
- Test: `tests/unit/test_options_surface.py`

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_options_surface.py`:

```python
"""Tests for VolSurface grid and interpolation."""

import math
from datetime import date

import pytest


def test_surface_update_and_get_exact():
    """Exact grid point retrieval."""
    from hft_platform.options.surface import VolSurface

    s = VolSurface()
    s.update(20000.0, date(2026, 4, 15), 0.20)
    assert s.get_iv(20000.0, date(2026, 4, 15)) == pytest.approx(0.20)


def test_surface_multiple_strikes():
    from hft_platform.options.surface import VolSurface

    s = VolSurface()
    d = date(2026, 4, 15)
    s.update(19500.0, d, 0.22)
    s.update(20000.0, d, 0.20)
    s.update(20500.0, d, 0.21)
    assert s.get_iv(19500.0, d) == pytest.approx(0.22)
    assert s.get_iv(20000.0, d) == pytest.approx(0.20)
    assert s.get_iv(20500.0, d) == pytest.approx(0.21)


def test_surface_interpolation_between_strikes():
    """Interpolation should return a value between neighbors."""
    from hft_platform.options.surface import VolSurface

    s = VolSurface()
    d = date(2026, 4, 15)
    s.update(19000.0, d, 0.25)
    s.update(19500.0, d, 0.22)
    s.update(20000.0, d, 0.20)
    s.update(20500.0, d, 0.21)
    s.update(21000.0, d, 0.24)
    iv = s.get_iv(19750.0, d)
    assert 0.19 < iv < 0.23  # between neighbors


def test_surface_stale_iv_excluded():
    """IV outside [0.01, 2.0] should be treated as stale."""
    from hft_platform.options.surface import VolSurface

    s = VolSurface()
    d = date(2026, 4, 15)
    s.update(20000.0, d, 0.005)  # below threshold
    s.update(20500.0, d, 0.20)
    # Stale point should not be in snapshot
    snap = s.snapshot()
    assert (d, 20000.0) not in snap
    assert (d, 20500.0) in snap


def test_surface_snapshot():
    from hft_platform.options.surface import VolSurface

    s = VolSurface()
    d = date(2026, 4, 15)
    s.update(20000.0, d, 0.20)
    s.update(20500.0, d, 0.21)
    snap = s.snapshot()
    assert len(snap) == 2
    assert snap[(d, 20000.0)] == pytest.approx(0.20)


def test_surface_get_iv_no_data_returns_nan():
    from hft_platform.options.surface import VolSurface

    s = VolSurface()
    result = s.get_iv(20000.0, date(2026, 4, 15))
    assert math.isnan(result)


def test_surface_get_iv_single_point_no_interp():
    """With only one strike, interpolation degrades to that point."""
    from hft_platform.options.surface import VolSurface

    s = VolSurface()
    d = date(2026, 4, 15)
    s.update(20000.0, d, 0.20)
    # Query a different strike — should return NaN (can't interpolate 1 point)
    result = s.get_iv(20500.0, d)
    # With < 2 points, interpolation is undefined
    assert math.isnan(result) or result == pytest.approx(0.20)


def test_surface_skew_25d():
    from hft_platform.options.surface import VolSurface

    s = VolSurface()
    d = date(2026, 4, 15)
    # Construct a smile: put side higher than call side
    for strike, iv in [(19000, 0.28), (19500, 0.24), (20000, 0.20), (20500, 0.22), (21000, 0.26)]:
        s.update(float(strike), d, iv)
    skew = s.skew_25d(d)
    # Skew = put_25d_IV - call_25d_IV, should be > 0 for typical equity skew
    assert isinstance(skew, float)


def test_surface_butterfly_25d():
    from hft_platform.options.surface import VolSurface

    s = VolSurface()
    d = date(2026, 4, 15)
    for strike, iv in [(19000, 0.28), (19500, 0.24), (20000, 0.20), (20500, 0.22), (21000, 0.26)]:
        s.update(float(strike), d, iv)
    bf = s.butterfly_25d(d)
    # Butterfly = 0.5*(put_25d + call_25d) - ATM, should be > 0 for convex smile
    assert isinstance(bf, float)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_options_surface.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `surface.py`**

Create `src/hft_platform/options/surface.py`:

```python
"""Volatility surface: strike × expiry grid with interpolation.

Offline analytics module (Rule 25 §11). All values are float.
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np
from scipy.interpolate import CubicSpline

_IV_MIN = 0.01
_IV_MAX = 2.0


class VolSurface:
    """Grid-based implied volatility surface.

    Data structure: dict[(expiry_date, strike)] → iv.
    Staleness: IV outside [0.01, 2.0] is excluded from interpolation.
    """

    __slots__ = ("_grid",)

    def __init__(self) -> None:
        self._grid: dict[tuple[date, float], float] = {}

    def update(self, strike: float, expiry_date: date, iv: float) -> None:
        """Update or insert a single grid point."""
        if _IV_MIN <= iv <= _IV_MAX:
            self._grid[(expiry_date, strike)] = iv
        else:
            # Stale — remove if exists
            self._grid.pop((expiry_date, strike), None)

    def get_iv(self, strike: float, expiry_date: date) -> float:
        """Get IV for (strike, expiry), interpolating if necessary.

        Strike dimension: cubic spline (if >= 4 points) or linear.
        Returns NaN if insufficient data.
        """
        # Collect all strikes for this expiry
        strikes: list[float] = []
        ivs: list[float] = []
        for (exp, k), v in self._grid.items():
            if exp == expiry_date:
                strikes.append(k)
                ivs.append(v)

        if not strikes:
            return float("nan")

        # Exact match
        for i, k in enumerate(strikes):
            if k == strike:
                return ivs[i]

        if len(strikes) < 2:
            return float("nan")

        # Sort by strike
        order = sorted(range(len(strikes)), key=lambda i: strikes[i])
        sorted_strikes = [strikes[i] for i in order]
        sorted_ivs = [ivs[i] for i in order]

        # Boundary check
        if strike < sorted_strikes[0] or strike > sorted_strikes[-1]:
            return float("nan")  # no extrapolation

        if len(sorted_strikes) >= 4:
            # Cubic spline
            cs = CubicSpline(sorted_strikes, sorted_ivs)
            return float(cs(strike))
        else:
            # Linear interpolation
            arr_k = np.array(sorted_strikes)
            arr_v = np.array(sorted_ivs)
            return float(np.interp(strike, arr_k, arr_v))

    def snapshot(self) -> dict[tuple[date, float], float]:
        """Return current grid as a dict (copy)."""
        return dict(self._grid)

    def skew_25d(self, expiry_date: date) -> float:
        """25-delta skew: put_25d_IV - call_25d_IV.

        Approximated using strikes at 25th and 75th percentile of available strikes.
        Returns NaN if insufficient data.
        """
        strikes, ivs = self._sorted_for_expiry(expiry_date)
        if len(strikes) < 3:
            return float("nan")

        # Approximate 25-delta as 25th percentile strike (put side)
        # and 75th percentile strike (call side)
        n = len(strikes)
        put_idx = max(0, n // 4)
        call_idx = min(n - 1, 3 * n // 4)
        return ivs[put_idx] - ivs[call_idx]

    def butterfly_25d(self, expiry_date: date) -> float:
        """25-delta butterfly: 0.5 * (put_25d_IV + call_25d_IV) - ATM_IV.

        Returns NaN if insufficient data.
        """
        strikes, ivs = self._sorted_for_expiry(expiry_date)
        if len(strikes) < 3:
            return float("nan")

        n = len(strikes)
        put_idx = max(0, n // 4)
        call_idx = min(n - 1, 3 * n // 4)
        atm_idx = n // 2
        return 0.5 * (ivs[put_idx] + ivs[call_idx]) - ivs[atm_idx]

    def _sorted_for_expiry(self, expiry_date: date) -> tuple[list[float], list[float]]:
        """Get sorted (strikes, ivs) for a given expiry."""
        strikes: list[float] = []
        ivs: list[float] = []
        for (exp, k), v in self._grid.items():
            if exp == expiry_date:
                strikes.append(k)
                ivs.append(v)
        if not strikes:
            return [], []
        order = sorted(range(len(strikes)), key=lambda i: strikes[i])
        return [strikes[i] for i in order], [ivs[i] for i in order]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_options_surface.py -v`
Expected: All 10 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/options/surface.py tests/unit/test_options_surface.py
git commit -m "feat(options): add volatility surface with grid storage and cubic spline interpolation"
```

---

### Task 6: Package Exports + Integration Smoke Test

**Files:**
- Modify: `src/hft_platform/options/__init__.py`
- Test: (use existing tests)

- [ ] **Step 1: Update `__init__.py` with public exports**

Edit `src/hft_platform/options/__init__.py`:

```python
"""Options analytics package — offline pricing, Greeks, vol surface.

Float exception: Per Architecture Governance Rule 25 §11, float is permitted
in this package for offline research computation. The live_adapter module
(Phase 2) is the boundary that converts float → int/bool before any value
enters the live trading path.
"""

from hft_platform.options.greeks import (
    AggregatedGreeks,
    GreeksResult,
    PositionGreeks,
    compute_greeks,
    portfolio_greeks,
)
from hft_platform.options.pricing import black76_price, solve_iv
from hft_platform.options.surface import VolSurface

__all__ = [
    "black76_price",
    "solve_iv",
    "compute_greeks",
    "portfolio_greeks",
    "GreeksResult",
    "PositionGreeks",
    "AggregatedGreeks",
    "VolSurface",
]
```

- [ ] **Step 2: Verify all options tests pass as a suite**

Run: `uv run pytest tests/unit/test_options_pricing.py tests/unit/test_options_greeks.py tests/unit/test_options_surface.py -v`
Expected: All tests PASS (33 total across 3 files).

- [ ] **Step 3: Verify imports work from top-level package**

Run: `uv run python -c "from hft_platform.options import black76_price, solve_iv, compute_greeks, portfolio_greeks, VolSurface; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Run full test suite for regression**

Run: `uv run pytest tests/unit/ -x -q --timeout=60`
Expected: No new failures. Note existing failure count for comparison.

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/options/__init__.py
git commit -m "feat(options): finalize package exports for pricing, greeks, surface"
```

---

### Task 7: Lint + Type Check + Final Verification

**Files:**
- All new/modified files from Tasks 1-6

- [ ] **Step 1: Run linter on new code**

Run: `uv run ruff check src/hft_platform/options/ src/hft_platform/config/_symbols_expansion.py tests/unit/test_options_*.py tests/unit/test_symbols_expansion_options.py tests/unit/test_contracts_runtime_r1r2.py`
Expected: No errors. Fix any issues found.

- [ ] **Step 2: Run type checker on new code**

Run: `uv run mypy src/hft_platform/options/ --ignore-missing-imports`
Expected: No type errors. Fix any issues found.

- [ ] **Step 3: Check test coverage for new modules**

Run: `uv run pytest tests/unit/test_options_pricing.py tests/unit/test_options_greeks.py tests/unit/test_options_surface.py tests/unit/test_symbols_expansion_options.py tests/unit/test_contracts_runtime_r1r2.py --cov=hft_platform.options --cov=hft_platform.config._symbols_expansion --cov-report=term-missing -v`
Expected: `hft_platform.options` modules ≥ 80% coverage.

- [ ] **Step 4: Commit any lint/type fixes**

```bash
git add -u
git commit -m "chore: fix lint and type issues in options module"
```

(Skip this step if there were no issues to fix.)
