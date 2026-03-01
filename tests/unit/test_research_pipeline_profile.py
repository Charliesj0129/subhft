from __future__ import annotations

import research.pipeline as research_pipeline


def _parse_run_args(*extra: str):
    parser = research_pipeline.build_parser()
    return parser.parse_args(
        [
            "run",
            "--alpha-id",
            "queue_imbalance",
            "--owner",
            "charlie",
            "--data",
            "research/data/processed/queue_imbalance/synthetic_qi_v1.npy",
            *extra,
        ]
    )


def test_vm_ul6_profile_applies_stricter_defaults() -> None:
    args = _parse_run_args("--validation-profile", "vm_ul6")
    notes: list[str] = []
    research_pipeline._apply_validation_profile(args, strict_mode=True, notes=notes)

    assert args.latency_profile_id == "sim_stress_v2026-02-26"
    assert args.local_decision_pipeline_latency_us == 1000
    assert args.min_stat_tests_pass == 3
    assert args.min_sharpe_oos_gate_d == 1.8
    assert args.max_abs_drawdown_gate_d == 0.10
    assert args.enforce_rust_benchmark_gate is True
    assert args.data_ul == 6
    assert args.required_data_provenance_fields == [
        "source",
        "generator",
        "seed",
        "created_at",
        "data_file",
        "split",
        "symbols",
    ]
    assert any("vm_ul6" in line for line in notes)


def test_vm_ul6_profile_keeps_explicit_user_overrides() -> None:
    args = _parse_run_args(
        "--validation-profile",
        "vm_ul6",
        "--min-sharpe-oos-gate-d",
        "2.3",
        "--latency-profile-id",
        "custom_profile",
        "--required-data-provenance-fields",
        "source",
        "seed",
        "--data-ul",
        "4",
    )
    notes: list[str] = []
    research_pipeline._apply_validation_profile(args, strict_mode=True, notes=notes)

    assert args.min_sharpe_oos_gate_d == 2.3
    assert args.latency_profile_id == "custom_profile"
    assert args.data_ul == 4
    assert args.required_data_provenance_fields == ["source", "seed"]
