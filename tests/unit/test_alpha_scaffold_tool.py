from __future__ import annotations

import research.tools.alpha_scaffold as alpha_scaffold


def test_normalize_data_fields_supports_csv_and_dedup() -> None:
    fields = alpha_scaffold._normalize_data_fields(["ofi_l1_ema8, spread_scaled", "spread_scaled", "l1_bid_qty"])
    assert fields == ("ofi_l1_ema8", "spread_scaled", "l1_bid_qty")


def test_render_impl_removes_todo_placeholders() -> None:
    content = alpha_scaffold.render_impl(
        alpha_id="ofi_template_test",
        paper_refs=["120"],
        complexity="O(1)",
        hypothesis="Signed OFI predicts short-horizon drift.",
        formula="alpha_t = zscore(ofi_l1_ema8_t)",
        data_fields=("ofi_l1_ema8",),
    )
    assert "TODO:" not in content
    assert 'hypothesis="Signed OFI predicts short-horizon drift."' in content
    assert 'formula="alpha_t = zscore(ofi_l1_ema8_t)"' in content
    assert 'data_fields=("ofi_l1_ema8",)' in content
    assert 'roles_used=("planner", "code-reviewer")' in content
    assert 'skills_used=("iterative-retrieval", "validation-gate")' in content


def test_render_readme_includes_data_fields_block() -> None:
    content = alpha_scaffold.render_readme(
        alpha_id="ofi_template_test",
        paper_refs=["120"],
        complexity="O(1)",
        hypothesis="h",
        formula="f",
        data_fields=("ofi_l1_ema8", "spread_scaled"),
    )
    assert "## Data Fields" in content
    assert "- `ofi_l1_ema8`" in content
    assert "- `spread_scaled`" in content
