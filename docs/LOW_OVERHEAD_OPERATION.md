# Low-overhead orchestration

The policy engine evaluates agent orchestration budgets. It does not validate
scientific results, prove model readiness, execute an experiment, or authorize
external effects. Keep it available for delegation and recovery; do not insert
it into every step of a fixed, already authorized native batch.

## Check at meaningful boundaries

Before delegation or a governed recovery attempt, persist the actual cumulative
observations and evaluate the public policy. Also check on substantive execution,
scope/authority changes, and when a real tool/time budget boundary is reached.
An unchanged progress poll is an observation, not another plan-act-reflect cycle.
Batch those observations without losing counts or treating unknown costs as zero.
Wait for a checkpoint writer to complete before evaluating or advancing its file.
Do not create new processes, caches or schedules just to track this accounting.

Use `python scripts/policy_engine.py evaluate CHECKPOINT --compact` for concise
operator output. This returns a tagged summary, not a complete PolicyDecision.
The evaluator and its exit codes remain unchanged; the default command still
returns the full result. Summary output must not be mistaken for budget approval
when `spawn_allowed` is false or the scope is action/goal stop.

## Reuse preparation without inventing readiness

Keep accepted source/configuration/model identities and reproducible receipts.
Reuse them when their relevant inputs and requirements are unchanged. Repeat
only the smallest check whose facts changed or whose contract requires freshness.
Dynamic state cannot be inferred from an old hash. A failed optional catalog or
connection probe is not proof that the intended model operation failed.

This adapter does not require resource snapshots, three-sample windows, temporary
snapshot allowances, or repeated human approval for previously authorized seeds.
Discretionary project checks require a concrete reason. When a human with the
relevant authority explicitly waives one, record the actual instruction and its
scope. Do not label that waiver as measured idle, forge samples, or override a
native permission/authorization gate. Repeating a broken check under newly
invented quotas is not recovery; preserve failure evidence and fix its cause.

## Keep runtime data out of source verification

Benchmark source fingerprints include Git-tracked files and non-ignored
untracked source, including dirty content, deletion markers, types and symlink
targets. Ignored runtime checkpoints, locks, evidence and virtual environments
are outside that source inventory. Tracked files remain fingerprinted even when
an ignore pattern matches them. Source inventory failures stop the benchmark.
Runtime evidence still needs its own manifest and raw-record integrity checks;
excluding it from a source fingerprint does not make it accepted evidence.

Use the repository's pinned Python environment for tests and doctor checks.
A globally installed incompatible package is an environment problem, not a
reason to weaken the package-version check. Date-dependent unit fixtures must
fix their clock while retaining actual authorization/evidence validation.

## Measure before claiming savings

Report wall time, file-read bytes and full versus summary output bytes separately.
These measurements can establish avoided work and smaller context artifacts.
They do not establish live model-token or monetary savings. Use actual provider
usage when available; otherwise token/cost fields remain null. Separate fixture
results, incident observations and completed production experiments. Benchmarking
this adapter must not resend an interrupted model request or rerun a scientific
experiment merely to manufacture a comparison.
