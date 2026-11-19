# Source code and lint policy

Use names, types, and tests to express what the code does. Put the reasons for
a design in the relevant [module guide](../modules/README.md),
[architecture document](../architecture/README.md), or [runbook](../runbooks/).
Describe the invariant, the failure it prevents, and the test that exercises it.
Python docstrings and rustdoc remain available for public interface contracts, generated CLI
help, and other consumers of `__doc__`.

| Rule | What blocks CI | How to resolve it |
| --- | --- | --- |
| HFT-D004 | Python and Rust source comments | Express the intent through code or put the rationale in the module documentation. |
| HFT-D005 | A new inline tool directive, a Ruff, mypy, or coverage suppression, a Ruff rule removal, a competing Ruff config, a Rust lint-suppression attribute, a new known financial precision violation, or GitHub Actions `continue-on-error` | Correct the issue at its source and keep the check blocking. |
| HFT-D002 | A new production check for pytest in `sys.modules` | Run the same production path in tests and inject external dependencies at their existing interfaces. |

For a recurring defect, add a precise rule for the offending syntax or
configuration and test both a rejected example and a legitimate use. For
behavior that depends on state or timing, write a regression test that
reproduces the failure. Use design review for temporary fallbacks whose
correctness depends on runtime context.

## Running the checks

`make code-policy` runs the source rules. `make lint` runs those rules and
Ruff. `make check` and `make ci` include lint, mypy, HFT discipline, and the
financial float gate. GitHub Actions runs `make lint` in the Lint & Format job. It compares PRs and
merge groups to the event's base commit and pushes to the previous commit.
Locally the default is `origin/main`. Set `CODE_POLICY_BASE` to the target
revision when checking work based on another branch.

The checker uses Python's tokenizer, the Rust grammar, the existing AST
discipline rule for pytest detection, and parsed lint, coverage, and workflow settings.
It scans tracked and new non-ignored Python and Rust files. Interpreter shebangs and
encoding declarations serve the Python loader. Existing tool directives are
matched to their source line in Git so new lines cannot reuse an inline waiver.
Historical research and golden fixtures are outside the scan. The protected
contracts, pricing, timebase, events, and discipline modules may retain prose
already present in the comparison revision. Remove obsolete directives when
their underlying issue is fixed.

The GitHub unit-test job builds its own Rust extension and runs independently
of the lint and typecheck jobs. This keeps test and coverage failures visible
during a lint failure. The latency benchmark step uses Bash with pipeline
failure propagation so its exit code survives writing the log through `tee`.
The nightly workflow starts at 18:30 UTC.

`make test-unit-ci` distributes CPU-bound unit tests with xdist work stealing.
Pytest-cov combines worker data before the global and domain coverage checks.
Use `make test-file FILE=...` or `make test-node NODE=...` for readable output
from a single test group.


## Financial precision

`make float-gate` checks float annotations and conversions in order, risk,
execution, and the market-data normalizer. It checks each match against
[scripts/float_gate_exemptions.yaml](../../scripts/float_gate_exemptions.yaml)
by file, exact code, and occurrence count. Comments have no effect on what
passes. The rules distinguish timestamps, durations, and dimensionless values
from scaled monetary values. The gate fails on a stale entry and HFT-D005
blocks new known-violation entries and increases to their occurrence counts.
