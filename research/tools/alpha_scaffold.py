from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ALPHAS_DIR = ROOT / "research" / "alphas"
TEMPLATES_DIR = ALPHAS_DIR / "_templates"


def main() -> int:
    parser = argparse.ArgumentParser(description="Scaffold a standard alpha artifact directory.")
    parser.add_argument("alpha_id", help="Immutable alpha id (e.g. ofi_mc_v2)")
    parser.add_argument("--paper", action="append", default=[], help="Paper reference (repeatable)")
    parser.add_argument("--complexity", default="O1", help="Complexity target, e.g. O1 or ON")
    parser.add_argument("--force", action="store_true", help="Overwrite existing files")
    args = parser.parse_args()

    alpha_id = normalize_alpha_id(args.alpha_id)
    complexity = normalize_complexity(args.complexity)
    alpha_dir = ALPHAS_DIR / alpha_id

    if alpha_dir.exists() and not args.force:
        raise SystemExit(f"Alpha directory already exists: {alpha_dir} (use --force to overwrite)")

    alpha_dir.mkdir(parents=True, exist_ok=True)
    (alpha_dir / "tests").mkdir(parents=True, exist_ok=True)

    write_file(alpha_dir / "__init__.py", f'"""Alpha package: {alpha_id}."""\n', force=args.force)
    write_file(
        alpha_dir / "README.md",
        render_readme(alpha_id=alpha_id, paper_refs=args.paper, complexity=complexity),
        force=args.force,
    )
    write_file(
        alpha_dir / "impl.py",
        render_impl(alpha_id=alpha_id, paper_refs=args.paper, complexity=complexity),
        force=args.force,
    )
    write_file(
        alpha_dir / "tests" / "test_logic.py",
        render_test_logic(alpha_id=alpha_id),
        force=args.force,
    )
    write_file(
        alpha_dir / "tests" / "test_anti_leak.py",
        render_test_anti_leak(alpha_id=alpha_id),
        force=args.force,
    )
    write_file(alpha_dir / "scorecard.json", json.dumps({}, indent=2) + "\n", force=args.force)
    write_file(alpha_dir / "backtest_report.json", json.dumps({}, indent=2) + "\n", force=args.force)
    write_file(alpha_dir / "CHANGELOG.md", f"# Changelog: {alpha_id}\n\n- Initial scaffold.\n", force=args.force)

    ensure_templates(alpha_id=alpha_id, paper_refs=args.paper, complexity=complexity)
    print(f"Scaffolded alpha artifact: {alpha_dir}")
    return 0


def normalize_alpha_id(alpha_id: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", alpha_id.strip()).lower().strip("_")
    if not normalized:
        raise ValueError("alpha_id must not be empty")
    return normalized


def normalize_complexity(value: str) -> str:
    text = value.strip().upper().replace("(", "").replace(")", "")
    mapping = {"O1": "O(1)", "ON": "O(N)"}
    return mapping.get(text, value)


def class_name(alpha_id: str) -> str:
    return "".join(part.capitalize() for part in alpha_id.split("_")) + "Alpha"


def write_file(path: Path, content: str, *, force: bool) -> None:
    if path.exists() and not force:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def render_readme(alpha_id: str, paper_refs: list[str], complexity: str) -> str:
    refs = ", ".join(paper_refs) if paper_refs else "N/A"
    return (
        f"# {alpha_id}\n\n"
        "## Hypothesis\n"
        "- Describe the targeted inefficiency.\n\n"
        "## Formula\n"
        "- Add formula (LaTeX/pseudocode).\n\n"
        "## Metadata\n"
        f"- `alpha_id`: `{alpha_id}`\n"
        f"- `paper_refs`: {refs}\n"
        f"- `complexity`: `{complexity}`\n"
    )


def render_impl(alpha_id: str, paper_refs: list[str], complexity: str) -> str:
    refs_literal = "(" + ", ".join(f'"{p}"' for p in paper_refs) + ("," if paper_refs else "") + ")"
    cls_name = class_name(alpha_id)
    return f'''from __future__ import annotations

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier


class {cls_name}:
    def __init__(self) -> None:
        self._signal = 0.0

    @property
    def manifest(self) -> AlphaManifest:
        return AlphaManifest(
            alpha_id="{alpha_id}",
            hypothesis="TODO: hypothesis",
            formula="TODO: formula",
            paper_refs={refs_literal},
            data_fields=(),
            complexity="{complexity}",
            status=AlphaStatus.DRAFT,
            tier=AlphaTier.ENSEMBLE,
            rust_module=None,
        )

    def update(self, *args, **kwargs) -> float:
        self._signal = 0.0
        return self._signal

    def reset(self) -> None:
        self._signal = 0.0

    def get_signal(self) -> float:
        return self._signal


ALPHA_CLASS = {cls_name}
'''


def render_test_logic(alpha_id: str) -> str:
    cls_name = class_name(alpha_id)
    return f"""from research.alphas.{alpha_id}.impl import {cls_name}


def test_manifest_alpha_id() -> None:
    alpha = {cls_name}()
    assert alpha.manifest.alpha_id == "{alpha_id}"
"""


def render_test_anti_leak(alpha_id: str) -> str:
    cls_name = class_name(alpha_id)
    return f"""from research.alphas.{alpha_id}.impl import {cls_name}


def test_update_is_deterministic() -> None:
    alpha = {cls_name}()
    a = alpha.update(x=1)
    b = alpha.update(x=1)
    assert a == b
"""


def ensure_templates(alpha_id: str, paper_refs: list[str], complexity: str) -> None:
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    readme_tmpl = TEMPLATES_DIR / "README.md.tmpl"
    impl_tmpl = TEMPLATES_DIR / "impl.py.tmpl"
    if not readme_tmpl.exists():
        readme_tmpl.write_text(render_readme(alpha_id=alpha_id, paper_refs=paper_refs, complexity=complexity))
    if not impl_tmpl.exists():
        impl_tmpl.write_text(render_impl(alpha_id=alpha_id, paper_refs=paper_refs, complexity=complexity))


if __name__ == "__main__":
    raise SystemExit(main())
