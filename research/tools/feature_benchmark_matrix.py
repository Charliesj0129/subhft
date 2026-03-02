#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def run_gate(env_overrides: dict[str, str], runs: int, extra: list[str]) -> dict:
    env = {**os.environ, **env_overrides}
    with tempfile.TemporaryDirectory() as td:
        json_path = str(Path(td) / 'perf_gate.json')
        cmd = [sys.executable, 'tests/benchmark/perf_regression_gate.py', '--runs', str(runs), '--json', json_path] + extra
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
        out = {'cmd': cmd, 'returncode': proc.returncode, 'stdout': proc.stdout, 'stderr': proc.stderr}
        try:
            out['json'] = json.loads(Path(json_path).read_text(encoding='utf-8'))
        except Exception:
            out['json'] = None
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description='Run research feature benchmark matrix (cold/warm/profile variants)')
    ap.add_argument('--runs', type=int, default=1)
    ap.add_argument('--out', default='outputs/research_feature_benchmark_matrix.json')
    args = ap.parse_args()
    matrix = {
        'baseline': run_gate({}, args.runs, []),
        'numba_warm': run_gate({'HFT_RESEARCH_NUMBA': '1'}, args.runs, []),
        'feature_engine_enabled': run_gate({'HFT_FEATURE_ENGINE_ENABLED': '1'}, args.runs, []),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(matrix, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'out': args.out, 'cases': list(matrix)}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
