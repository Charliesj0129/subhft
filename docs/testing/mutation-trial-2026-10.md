# Mutation trial: `risk/` and `recorder/` (2026-10)

**Status:** one-off trial, `origin/main` `53f8b815`. Nothing in `src/` or `tests/` was changed by it.

A mutation trial changes one operator or constant at a time in a module and re-runs that module's own tests. A mutant
that the tests do not notice (it *survives*) points at behaviour no test pins. This trial covers 10 small modules:
5 in `src/hft_platform/risk/` and 5 in `src/hft_platform/recorder/`.

## Summary

| | count |
|---|---|
| modules | 10 |
| mutants run (every candidate; the cap of 60 per module was never reached) | 124 |
| killed | 93 |
| survived | 31 |
| survivors that are test gaps | 18 |
| survivors that are equivalent (no observable difference) | 5 |
| survivors that change only a log field or a log level | 7 |
| tool artifacts (a mutation inside a type annotation) | 1 |

Kill rate: 93 / 124 = 75.0 %; leaving out the 5 equivalent mutants and the 1 artifact, 93 / 118 = 78.8 %. Both are
low-resolution numbers (see [What this does not show](#what-this-does-not-show)): the useful output is the list of
test gaps below, not the percentage.

Three of the gaps are on a safety path and are the ones to close first:

1. `risk/halt_flattener.py:91`: no test pins the side for a **1-lot long**. With `net_qty > 1` in place of
   `net_qty > 0` a long of exactly 1 is "flattened" with a **BUY** (it is added to, not closed). The auto-flatten is
   opt-in (`HFT_HALT_AUTO_FLATTEN`), but when it runs it runs on HALT.
2. `recorder/shard_claim.py:60` and `:73`: the two fail-closed branches of `try_claim` (the claim file cannot be
   opened; `flock` fails with something other than "already locked") return `False`. A mutant that returns `True`
   there survives, so nothing checks that a worker which could not take the lock refuses to process the WAL file.
   Two workers on one file is the double-replay the claim exists to prevent.

## Method

- **Tool.** `mutmut` was the plan, but it is not installed here and a network fetch is out of scope for this work, so a
  small stdlib-only runner was used (appendix). **This is a deviation from the plan; the numbers are not comparable to
  `mutmut`'s**, which also mutates statements and return values.
- **Mutants.** Token-level, from `tokenize`: comparison flips (`<` / `<=`, `>` / `>=`, `==` / `!=`), `and` / `or`,
  `not` removal, `True` / `False`, `+` / `-`, and an integer literal plus one. Tokens inside strings and comments are
  never touched; lines that start a `def`, `class`, import or decorator are skipped, and so is a sign directly after an operator or
  an opening bracket.
- **Tests.** Per module, only the unit-test files that name it (listed below), with `-x --timeout=10 --no-cov` and
  `PYTHONDONTWRITEBYTECODE=1`; the module's `__pycache__` entries were deleted before each mutant (Python reuses a
  cached `.pyc` when two mutants have the same size and are written in the same second, which can report a surviving
  mutant as killed). The unmutated module passed
  its tests once before each module's mutants were run, and the file was restored byte-exact afterwards (sha1
  compared).
- **Triage.** Every survivor was classified by reading the surrounding code and the module's tests:
  *gap* (an observable behaviour no test pins), *equivalent* (no observable difference), *cosmetic* (only a log
  field or level changes), *artifact* (the tool mutated an annotation).

## Results by module

| module | tests used | candidates | killed | survived |
|---|---|---|---|---|
| `risk/kill_switch.py` | `test_system_killswitch_async.py`, `test_boot_halt_does_not_restart_into_trading.py` | 7 | 7 | 0 |
| `risk/position_sync.py` | `test_risk_position_sync.py` | 0 | 0 | 0 |
| `risk/liquidity_gate.py` | `test_liquidity_gate.py` | 8 | 7 | 1 |
| `risk/halt_flattener.py` | `test_halt_flattener.py` | 14 | 8 | 6 |
| `risk/fast_gate.py` | `test_fast_gate_coverage.py`, `test_fast_gate_missing_lines.py`, `test_risk_fast_gate.py` | 39 | 33 | 6 |
| `recorder/wal_first.py` | `test_wal_first_writer.py` | 7 | 7 | 0 |
| `recorder/mode.py` | `test_recorder_mode.py` | 1 | 0 | 1 |
| `recorder/replay_contract.py` | `test_replay_contract.py` | 8 | 5 | 3 |
| `recorder/shard_claim.py` | `test_shard_claim.py` | 14 | 9 | 5 |
| `recorder/disk_monitor.py` | `test_recorder_disk_monitor.py`, `test_disk_monitor.py`, `test_disk_monitor_hysteresis.py` | 26 | 17 | 9 |
| **total** | | **124** | **93** | **31** |

All test files are under `tests/unit/`. `risk/position_sync.py` has no token the runner mutates (assignments and a lock),
so that row says nothing about its tests (its docstring says the class is not wired into production). `risk/kill_switch.py` and `recorder/wal_first.py` have no survivors.

## Survivors

Line numbers are in the unmutated files at `53f8b815`. "Priority" ranks only the gaps: **high** = a safety or
fail-closed behaviour, **medium** = a limit, threshold or default that decides behaviour, **low** = bookkeeping.

### Test gaps (18)

| # | where | mutant | what is not pinned | priority | test that would kill it |
|---|---|---|---|---|---|
| 1 | `risk/halt_flattener.py:91` | `net_qty > 0` to `net_qty > 1` | closing side of a **1-lot long** (becomes BUY) | high | `net_qty=+1` on HALT emits SELL qty 1; `net_qty=-1` emits BUY qty 1 |
| 2 | `recorder/shard_claim.py:60` | `return False` to `True` | a claim file that cannot be opened must not be claimed | high | make `os.open` raise `OSError`; assert `try_claim` is `False` |
| 3 | `recorder/shard_claim.py:73` | `return False` to `True` | an `flock` error other than "already locked" must not be claimed | high | patch `fcntl.flock` to raise `OSError`; assert `False` and the fd is closed |
| 4 | `risk/halt_flattener.py:104` | `price=0` to `price=1` | the flatten intent is a market order (`price=0`) | medium | assert `intent.price == 0` |
| 5 | `risk/fast_gate.py:57` | `max_qty=100` to `101` | the default quantity limit | medium | `FastGate()` rejects qty 101 and accepts 100 |
| 6-8 | `recorder/disk_monitor.py:189`, `:196`, `:202` | `>=` to `>` | the hysteresis exit band at exactly the exit size (HALT, CRITICAL, WARN): the level must be kept at `size == threshold * (1 - band)` and left one byte below | medium | size exactly on each exit threshold keeps the level; one byte under leaves it |
| 9-10 | `recorder/disk_monitor.py:167` (two tokens) | `1024` to `1025` | the bytes-to-MB conversion | medium | a file of exactly 1 MiB reports `1.0` |
| 11 | `recorder/shard_claim.py:43` | `exist_ok=True` to `False` | a second claimer (or a restart) over an existing claims directory must not raise | medium | construct two `FileClaimRegistry` objects on one directory |
| 12-14 | `recorder/replay_contract.py:32`, `:33`, `:34` | defaults of `getattr(loader, "_strict_order" / "_dedup_enabled" / "_manifest_enabled", ...)` | the verdict for a loader that lacks those attributes | low | a bare object with only `archive_dir`: no violation except the archive one |
| 15 | `risk/fast_gate.py:58` | `create_shm=False` to `True` | the default is to attach, not to create, the kill-switch segment | low | construct `FastGate()` with the segment absent/present and assert which |
| 16 | `risk/halt_flattener.py:83` | `getattr(pos, "net_qty", 0)` default to `1` | a position object without `net_qty` is skipped, not flattened | low | a position stub without `net_qty`; assert nothing is emitted |
| 17-18 | `risk/halt_flattener.py:55`, `:62` | initial id `0` to `1`; step `1` to `2` | the numbering of `intent_id` (only uniqueness is asserted today) | low | assert ids are `1, 2, 3` or document that only uniqueness matters |

### Equivalent (5)

| where | mutant | why it cannot be seen |
|---|---|---|
| `risk/halt_flattener.py:91` | `net_qty > 0` to `>= 0` | `net_qty == 0` is skipped at `:84-85` before this line |
| `risk/fast_gate.py:85` | warm-up quantity `1` to `2` | JIT warm-up call; only the argument types matter |
| `risk/fast_gate.py:91`, `:105` | kill-switch segment `size=1` to `2` | the kill-switch byte is index 0; a larger segment behaves the same |
| `recorder/disk_monitor.py:73` | `_running = False` to `True` in `__init__` | only `_loop` reads it and `start()` sets it first |

### Log-only (7)

`risk/liquidity_gate.py:47` (`round(..., 2)` to `3` in a log field); `recorder/mode.py:29` (`stacklevel=2` to `3`);
`recorder/shard_claim.py:109`, `:123` (the `recovered` count, which is only logged); `recorder/disk_monitor.py:129`
(`new_level > OK` to `>=` changes the level of the recovery log line), `:134`, `:137` (rounding in log fields).
None changes behaviour; a test would have to capture log output, which is not worth it for these.

### Tool artifact (1)

`risk/fast_gate.py:178`: `Literal[False]` in a return annotation on a continuation line of a `def`. The runner skips
only lines that *start* a `def`, so this annotation was mutated; it is not code.

## What this does not show

- **Only the module's own tests ran.** A survivor may be caught by another test file (for instance an integration
  test); the trial reports what the module's unit tests pin, not what the whole suite does.
- **Token-level mutants only.** No statement deletion, no changed return value, no swapped argument. `mutmut` would
  find more survivors, and the kill rate above is an upper bound for it.
- **Equivalent mutants were judged by reading**, not proved.
- **Ten of the 32 files** in the two packages, chosen because they are small and have a named unit-test file.
  `risk/engine.py`, `risk/validators.py`, `risk/storm_guard.py`, `recorder/writer.py`, `recorder/batcher.py` and
  `recorder/wal.py` were not tried; they are where a trial would matter most and where a per-mutant test run is slow.
- **A timeout counts as a kill.** The runner prints only KILLED or SURVIVED, so a mutant that hangs and is stopped by `--timeout=10` is not told apart from one a test failed.

## Follow-up (not done here)

1. Close gaps 1-3 with focused tests (the table names each); gaps 4-11 next.
2. When network access for tooling is allowed, rerun with `mutmut` on the larger modules and compare.
3. Decide whether a mutation run belongs in the scheduled CI (not proposed here; the cost per module is minutes).

## Appendix: the runner

The runner that produced the numbers (the two environment-specific constants at the top were fixed paths in the
original run; here they are the current directory and the running interpreter). It mutates the module in place and
restores it byte-exact in a `finally` block, so run it in a disposable worktree and never in a tree with uncommitted
work.

```python
"""Token-level mutation trial for one module at a time (stdlib only, no network).

usage: python mutate.py <module path relative to cwd> <cap> <test file> [<test file> ...]
Writes one line per mutant to stdout; restores the module byte-exact and prints the sha1 check.
Operators: comparison flips, and/or swap, `not` removal, True/False swap, int +1, + <-> -.
Tokens inside strings/comments/annotations are never touched (tokenize yields them as STRING/COMMENT).
"""
import hashlib
import io
import subprocess
import sys
import tokenize
from pathlib import Path

WT = Path.cwd()  # a disposable worktree: the module is rewritten in place
PY = sys.executable  # must be able to run the project's tests

SWAP = {"<": "<=", "<=": "<", ">": ">=", ">=": ">", "==": "!=", "!=": "==", "and": "or", "or": "and",
        "True": "False", "False": "True", "+": "-", "-": "+"}


def candidates(src: str):
    toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    depth_def = False
    out = []
    for i, t in enumerate(toks):
        s = t.string
        prev = toks[i - 1].string if i else ""
        nxt = toks[i + 1].string if i + 1 < len(toks) else ""
        # skip `def f(...) -> X:` annotation region and decorators cheaply: only touch tokens on lines that are not
        # a def/class header or an import
        line = t.line.lstrip()
        if line.startswith(("def ", "async def ", "class ", "import ", "from ", "@")):
            continue
        new = None
        if t.type == tokenize.OP and s in SWAP:
            # unary minus/plus directly after an operator or open paren is left alone (sign flip is noisy)
            if s in "+-" and (prev in ("(", ",", "=", "[", "return", ":") or toks[i - 1].type == tokenize.OP and prev not in (")", "]")):
                continue
            new = SWAP[s]
        elif t.type == tokenize.NAME and s in ("and", "or", "True", "False"):
            new = SWAP[s]
        elif t.type == tokenize.NAME and s == "not":
            if nxt == "in" or prev == "is":
                continue
            new = ""
        elif t.type == tokenize.NUMBER and s.isdigit():
            new = str(int(s) + 1)
        if new is None:
            continue
        out.append((t.start, t.end, s, new))
    return out


def main() -> int:
    rel, cap, tests = sys.argv[1], int(sys.argv[2]), sys.argv[3:]
    path = WT / rel
    orig = path.read_bytes()
    h0 = hashlib.sha1(orig).hexdigest()
    src = orig.decode("utf-8")
    lines = src.splitlines(keepends=True)
    cands = candidates(src)
    if len(cands) > cap:
        step = len(cands) / cap
        cands = [cands[int(i * step)] for i in range(cap)]
    env = {"PYTHONPATH": "src", "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
    base = subprocess.run([PY, "-B", "-m", "pytest", "--no-cov", "-p", "no:cacheprovider", "-q", "--timeout=10", *tests],
                          cwd=WT, env=env, capture_output=True, text=True)
    print(f"BASELINE rc={base.returncode} :: {base.stdout.strip().splitlines()[-1] if base.stdout.strip() else base.stderr[-200:]}")
    if base.returncode != 0:
        return 2
    killed = survived = 0
    try:
        for (sr, sc), (er, ec), old, new in cands:
            mutated = list(lines)
            row = mutated[sr - 1]
            mutated[sr - 1] = row[:sc] + new + row[ec:] if sr == er else row
            path.write_text("".join(mutated), encoding="utf-8")
            pc = path.parent / "__pycache__"
            for f in pc.glob(path.stem + "*"):
                f.unlink()
            r = subprocess.run([PY, "-B", "-m", "pytest", "--no-cov", "-p", "no:cacheprovider", "-q", "-x", "--timeout=10", *tests],
                               cwd=WT, env=env, capture_output=True, text=True)
            verdict = "KILLED" if r.returncode != 0 else "SURVIVED"
            killed += verdict == "KILLED"
            survived += verdict == "SURVIVED"
            print(f"{verdict:8} {rel}:{sr} `{old}` -> `{new or '(removed)'}` | {lines[sr-1].strip()[:90]}", flush=True)
    finally:
        path.write_bytes(orig)
        for f in (path.parent / "__pycache__").glob(path.stem + "*"):
            f.unlink()
    print(f"RESULT {rel} killed={killed} survived={survived} candidates_total={len(candidates(src))} restored={hashlib.sha1(path.read_bytes()).hexdigest() == h0}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Usage, from the worktree root, with the project's virtualenv Python: `python mutate.py
src/hft_platform/risk/halt_flattener.py 60 tests/unit/test_halt_flattener.py` (the runner sets `PYTHONPATH=src` for the
tests it starts).
