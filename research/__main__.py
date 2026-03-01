"""Single entry point: python -m research <command> [args]

Factory commands (layout / governance):
  init              Initialize canonical directory layout
  clean             Remove __pycache__ and compiled artifacts
  converge-tools    Move non-core scripts to research/tools/legacy
  audit             Audit pipeline contract (writes factory_audit.json)
  index             Build machine-readable alpha index
  optimize          One-flow: init -> converge-tools -> clean -> audit -> index

Pipeline commands (alpha lifecycle -- strict / promotable):
  run               Strict SOP: factory optimize -> Gate A/B/C -> Gate D/E -> index
  triage            Debug mode (requires HFT_RESEARCH_ALLOW_TRIAGE=1; never promotable)

Scaffold command:
  scaffold <alpha_id> [--paper ref] [--complexity O1|ON]
                    Scaffold a new governed alpha package under research/alphas/

Paper commands:
  fetch-paper <arxiv_id>          Fetch and index a paper from arXiv
  search-papers <query> [--max N] Search arXiv by query
  paper-to-prototype <paper_ref>  Scaffold prototype and link paper->alpha

Paper-trade commands:
  record-paper --alpha-id <id> ...     Record one paper-trade session
  summarize-paper --alpha-id <id>      Summarize paper-trade sessions

Data governance commands:
  stamp-data-meta <data.npy>           Create metadata sidecar
  validate-data-meta <data.npy>        Validate metadata sidecar

Maintenance commands:
  audit-note-citations                 Audit note citation completeness
  backfill-note-citations              Backfill normalized citation headers
  triage-pyspy                         Parse pyspy SVGs and rank hotspots
"""
from __future__ import annotations

import sys

_FACTORY_CMDS = frozenset({"init", "clean", "converge-tools", "audit", "index", "optimize"})
_PIPELINE_CMDS = frozenset({"run", "triage"})
_SCAFFOLD_CMD = "scaffold"
_PAPER_CMDS = frozenset({"fetch-paper", "search-papers"})
_PAPER_PROTO_CMD = "paper-to-prototype"
_PAPER_TRADE_CMDS = frozenset({"record-paper", "summarize-paper"})
_DATA_GOV_CMDS = frozenset({"stamp-data-meta", "validate-data-meta"})
_MAINT_CMDS = frozenset({"audit-note-citations", "backfill-note-citations", "triage-pyspy"})

_USAGE = """\
Usage: python -m research <command> [options]

Factory:  init | clean | converge-tools | audit | index | optimize
Pipeline: run  | triage
Scaffold: scaffold <alpha_id>
Paper:    fetch-paper <arxiv_id> | search-papers <query>
Paper-prototype: paper-to-prototype <paper_ref>
Paper-trade: record-paper --alpha-id <id> | summarize-paper --alpha-id <id>
Data-governance: stamp-data-meta <data> | validate-data-meta <data>
Maintenance: audit-note-citations | backfill-note-citations | triage-pyspy

Run 'python -m research <command> --help' for per-command help.
"""


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        print(_USAGE)
        return 0

    cmd = sys.argv[1]

    if cmd in _FACTORY_CMDS:
        from research.factory import main as _factory_main

        return int(_factory_main())

    if cmd in _PIPELINE_CMDS:
        from research.pipeline import main as _pipeline_main

        return int(_pipeline_main())

    if cmd == _SCAFFOLD_CMD:
        # Remove "scaffold" so alpha_scaffold's positional arg is alpha_id at [1].
        sys.argv.pop(1)
        from research.tools.alpha_scaffold import main as _scaffold_main

        return int(_scaffold_main())

    if cmd in _PAPER_CMDS:
        from research.tools.fetch_paper import main as _fetch_main

        return int(_fetch_main())

    if cmd == _PAPER_PROTO_CMD:
        from research.tools.paper_prototype import main as _paper_proto_main

        return int(_paper_proto_main())

    if cmd in _PAPER_TRADE_CMDS:
        from research.tools.paper_trade import main as _paper_trade_main

        return int(_paper_trade_main())

    if cmd in _DATA_GOV_CMDS:
        from research.tools.data_governance import main as _data_gov_main

        return int(_data_gov_main())

    if cmd in _MAINT_CMDS:
        from research.tools.maintenance import main as _maint_main

        return int(_maint_main())

    print(f"Unknown command: {cmd!r}\n\n{_USAGE}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
