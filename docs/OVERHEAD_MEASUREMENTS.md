# Orchestration overhead measurements

Measured on 2026-10-30 with the adapter's pinned Python environment and shared
package 0.5.1. All measurements are offline: no model generation, role promotion,
scientific rerun or provider-token benchmark was performed.

## Reproduction

Run `python scripts/benchmark_overhead.py --output NEW_HOME_RESULT.json` in the
pinned environment. Add `--runtime-mib 256` for the larger fixture. The output
must be a new file inside CODEX_HOME. An optional `--checkpoint` measures parsing
of an existing home checkpoint without evaluating or mutating it. Three timing
samples are retained for each implementation. Private raw outputs and host
checkpoint contents are not part of this repository.

| Measurement | Before | After | Interpretation |
|---|---:|---:|---|
| Source fingerprint, 64 MiB ignored runtime, median | 0.0789 s | 0.1037 s | Git startup makes this small cached case about 31% slower. |
| Source fingerprint, 256 MiB ignored runtime, median | 0.3110 s | 0.0864 s | About 72% less wall time in this larger case. |
| Python file bytes hashed, 64 MiB fixture | 67,108,882 | 18 | Runtime bytes excluded; Git metadata I/O is not counted here. |
| Python file bytes hashed, 256 MiB fixture | 268,435,474 | 18 | Tracked and non-ignored source remain covered. |
| Same continue decision, operator output | 1,124 bytes | 298 bytes | About 73.5% smaller; not measured provider-token savings. |

One live checkpoint was 218,807,660 bytes. Three read-only JSON parsing samples
were 1.1358, 1.1198 and 1.2000 seconds (median 1.1358). That state was unchanged before
and after measurement. No history, budgets or accepted evidence were removed.
This supports batching unchanged observations; it does not justify skipping a
real budget/authority boundary or claiming that every policy check costs this
same amount.

## Error diagnosis

An initial 124-test run under a global interpreter had two failures, one error
and three skips. The package-contract failure correctly detected shared package
0.6.0 where this adapter pins 0.5.1; using the pinned environment addresses that
environment mismatch without weakening the check. A raw-evidence test depended
on a stale calendar fixture; its clock is now fixed while actual validation
remains active. Source fingerprinting tried to read live runtime files under
the repository, including a locked temporary file; the new source inventory
excludes ignored runtime artifacts while still covering tracked, dirty and
non-ignored untracked source. Inventory failures remain explicit errors.

The final corrected suite ran 136 tests with four platform skips and no failures. Keep that
test result separate from the controlled timing comparison: interpreter and
temporary-directory placement also changed, so total-suite duration is not a
valid isolated speedup measurement.

Actual orchestration-model tokens and monetary costs remain unknown in these
measurements. There is no conversion from bytes into claimed billed tokens.
Likewise, a model-service catalog timeout is a separate service/dependency
failure; the generic policy engine does not itself require resource snapshots.
