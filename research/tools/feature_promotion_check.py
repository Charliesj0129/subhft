#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from hft_platform.feature.profile import load_feature_profile_registry
from hft_platform.alpha.validation import ValidationConfig, run_alpha_validation
from hft_platform.alpha.promotion import PromotionConfig, promote_alpha


def main() -> int:
    ap = argparse.ArgumentParser(description='Run research feature promotion checks (validation + optional promotion)')
    ap.add_argument('--alpha-id', required=True)
    ap.add_argument('--data', nargs='+', required=True)
    ap.add_argument('--profiles', default='config/feature_profiles.yaml')
    ap.add_argument('--owner', default='research')
    ap.add_argument('--promote', action='store_true')
    ap.add_argument('--out', help='JSON output path')
    args = ap.parse_args()

    prof_reg = load_feature_profile_registry(args.profiles)
    prof_errors = prof_reg.validate()

    validation = run_alpha_validation(ValidationConfig(alpha_id=args.alpha_id, data_paths=list(args.data)))
    result = {
        'alpha_id': args.alpha_id,
        'feature_profiles_path': args.profiles,
        'feature_profile_validation_errors': prof_errors,
        'validation': validation.to_dict(),
    }
    if args.promote and validation.passed and not prof_errors:
        promo = promote_alpha(PromotionConfig(alpha_id=args.alpha_id, owner=args.owner))
        result['promotion'] = promo.to_dict()

    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text, encoding='utf-8')
    print(text)
    return 0 if validation.passed and not prof_errors else 1


if __name__ == '__main__':
    raise SystemExit(main())
