#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description='Render markdown summary from promotion/validation JSON artifact')
    ap.add_argument('input')
    ap.add_argument('--out', help='Markdown output path')
    args = ap.parse_args()

    data = json.loads(Path(args.input).read_text(encoding='utf-8'))
    lines = ['# Research Feature Promotion Report', '']
    lines.append(f"- alpha_id: `{data.get('alpha_id')}`")
    val = data.get('validation', {})
    lines.append(f"- validation_passed: `{val.get('passed')}`")
    prof_errs = data.get('feature_profile_validation_errors') or []
    lines.append(f"- feature_profile_errors: `{len(prof_errs)}`")
    if prof_errs:
        lines.append('')
        lines.append('## Profile Errors')
        for e in prof_errs:
            lines.append(f'- {e}')
    if 'promotion' in data:
        promo = data['promotion']
        lines.append('')
        lines.append('## Promotion')
        lines.append(f"- approved: `{promo.get('approved')}`")
        lines.append(f"- canary_weight: `{promo.get('canary_weight')}`")
        for r in promo.get('reasons', []) or []:
            lines.append(f'- reason: {r}')
    text = '\n'.join(lines) + '\n'
    if args.out:
        Path(args.out).write_text(text, encoding='utf-8')
    print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
