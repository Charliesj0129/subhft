"""Unit tests for _update_manifest_status in hft_platform.alpha.validation.

Tests cover:
- Successful status update from DRAFT to each gate status.
- Idempotency: calling again with same target is a no-op (returns False).
- File not found: returns False without raising.
- Pattern not present in file: returns False.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hft_platform.alpha.validation import _update_manifest_status

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_impl(tmp_path: Path, alpha_id: str, status: str = "DRAFT") -> Path:
    """Create a minimal impl.py for *alpha_id* inside *tmp_path*."""
    alpha_dir = tmp_path / "research" / "alphas" / alpha_id
    alpha_dir.mkdir(parents=True)
    impl = alpha_dir / "impl.py"
    impl.write_text(
        f"""from research.registry.schemas import AlphaManifest, AlphaStatus

_MANIFEST = AlphaManifest(
    alpha_id="{alpha_id}",
    hypothesis="test",
    formula="x",
    paper_refs=(),
    data_fields=("mid_price",),
    complexity="O(1)",
    status=AlphaStatus.{status},
)
""",
        encoding="utf-8",
    )
    return impl


# ---------------------------------------------------------------------------
# Tests — successful status transitions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target_status",
    ["GATE_A", "GATE_B", "GATE_C", "GATE_D", "GATE_E", "PRODUCTION"],
)
def test_update_manifest_status_from_draft(tmp_path: Path, target_status: str) -> None:
    """Status line should be replaced and True returned."""
    alpha_id = "test_alpha"
    impl = _make_impl(tmp_path, alpha_id, status="DRAFT")

    result = _update_manifest_status(alpha_id, target_status, tmp_path)

    assert result is True
    content = impl.read_text(encoding="utf-8")
    assert f"status=AlphaStatus.{target_status}" in content
    assert "status=AlphaStatus.DRAFT" not in content


def test_update_manifest_status_intermediate_transition(tmp_path: Path) -> None:
    """Transitioning from GATE_A to GATE_B should work correctly."""
    alpha_id = "my_alpha"
    impl = _make_impl(tmp_path, alpha_id, status="GATE_A")

    result = _update_manifest_status(alpha_id, "GATE_B", tmp_path)

    assert result is True
    content = impl.read_text(encoding="utf-8")
    assert "status=AlphaStatus.GATE_B" in content
    assert "status=AlphaStatus.GATE_A" not in content


# ---------------------------------------------------------------------------
# Tests — idempotency
# ---------------------------------------------------------------------------


def test_update_manifest_status_already_at_target_returns_false(tmp_path: Path) -> None:
    """No-op when the status is already the desired target."""
    alpha_id = "idem_alpha"
    _make_impl(tmp_path, alpha_id, status="GATE_C")

    result = _update_manifest_status(alpha_id, "GATE_C", tmp_path)

    assert result is False


def test_update_manifest_status_idempotent_double_call(tmp_path: Path) -> None:
    """Second call with same target after successful first call is a no-op."""
    alpha_id = "double_alpha"
    impl = _make_impl(tmp_path, alpha_id, status="DRAFT")

    first = _update_manifest_status(alpha_id, "GATE_A", tmp_path)
    second = _update_manifest_status(alpha_id, "GATE_A", tmp_path)

    assert first is True
    assert second is False
    content = impl.read_text(encoding="utf-8")
    # Exactly one occurrence of the status line.
    assert content.count("status=AlphaStatus.GATE_A") == 1


# ---------------------------------------------------------------------------
# Tests — file not found
# ---------------------------------------------------------------------------


def test_update_manifest_status_file_not_found_returns_false(tmp_path: Path) -> None:
    """Missing impl.py must return False without raising."""
    result = _update_manifest_status("nonexistent_alpha", "GATE_A", tmp_path)
    assert result is False


def test_update_manifest_status_alpha_dir_missing(tmp_path: Path) -> None:
    """No alpha directory at all — must return False."""
    result = _update_manifest_status("ghost", "GATE_B", tmp_path)
    assert result is False


# ---------------------------------------------------------------------------
# Tests — pattern not found in file
# ---------------------------------------------------------------------------


def test_update_manifest_status_no_pattern_in_file(tmp_path: Path) -> None:
    """If impl.py exists but has no status=AlphaStatus.<X> line, return False."""
    alpha_id = "no_pattern_alpha"
    alpha_dir = tmp_path / "research" / "alphas" / alpha_id
    alpha_dir.mkdir(parents=True)
    impl = alpha_dir / "impl.py"
    impl.write_text("# no manifest here\n", encoding="utf-8")

    result = _update_manifest_status(alpha_id, "GATE_A", tmp_path)
    assert result is False
    # File content must not change.
    assert impl.read_text(encoding="utf-8") == "# no manifest here\n"


# ---------------------------------------------------------------------------
# Tests — content integrity
# ---------------------------------------------------------------------------


def test_update_manifest_status_only_replaces_status_line(tmp_path: Path) -> None:
    """Surrounding content must be preserved after replacement."""
    alpha_id = "content_alpha"
    impl = _make_impl(tmp_path, alpha_id, status="DRAFT")
    original_lines = impl.read_text(encoding="utf-8").splitlines()

    _update_manifest_status(alpha_id, "GATE_A", tmp_path)

    updated_lines = impl.read_text(encoding="utf-8").splitlines()
    # Same number of lines.
    assert len(original_lines) == len(updated_lines)
    # All lines other than the status line must be identical.
    for orig, updated in zip(original_lines, updated_lines):
        if "AlphaStatus" not in orig:
            assert orig == updated


def test_update_manifest_status_returns_true_and_file_written(tmp_path: Path) -> None:
    """Verify the returned bool aligns with the actual file state."""
    alpha_id = "write_verify"
    impl = _make_impl(tmp_path, alpha_id, status="DRAFT")

    changed = _update_manifest_status(alpha_id, "GATE_E", tmp_path)

    assert changed is True
    assert "status=AlphaStatus.GATE_E" in impl.read_text(encoding="utf-8")
