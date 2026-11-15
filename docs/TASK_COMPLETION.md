# Completion and deployment checks

These read-only checks support the existing task, not a second executor, budget,
scheduler or authorization gate. Current user instructions, repository contracts
and native controls retain precedence. Routine reads need no new contract file.

## Completion must match the task

Use `scripts/task_completion.py` (also deployed in maintenance/runtime) when a
completion record needs machine checks. Keep the contract in the task's existing
evidence area. Pin its raw SHA-256 before verification.

| Profile | Required evidence | What a pass means |
|---|---|---|
| command | Every required command's terminal receipt and complete log | Required commands and wrapper finalization passed |
| artifact | Named artifacts and exact hashes | Requested bytes exist unchanged, not semantic quality |
| ui | Original entry, action and outcome | Trusted adapter verifies this operation and targets |
| handoff | Delivery and receiver_ack | Trusted adapter verifies delivery and receiving outcome |

Reload/restart are extra requirements only when the task requests them. Human
acceptance is required only by an applicable contract, not by every engineering
task. Set require_human=true with a real_user event when it is actually required.

Contract schema_version=1 requires task_id, version, environment, profile, and a
nonempty requirements list with unique ids. Every listed requirement is mandatory:
- command: type, id, receipt (relative .json path), sha256, exact command list, cwd.
- artifact: type, id, path (relative), sha256.
- event: type, id, path, sha256, adapter, phase, target, mechanism, origin.
  Event contracts also require operation_id. The host must bind this same
  operation across entry/action/outcome or delivery/acknowledgment.

CLI usage:

```sh
python scripts/task_completion.py contract.json --contract-sha256 <SHA256> --evidence-root <TASK_EVIDENCE>
```

The CLI returns JSON and exit 0 only for complete. Failure or unknown returns 1;
it never retries or changes the underlying task. Execution authority stays false.

## Trust and failure boundaries

A hash proves byte integrity, not authorship. The caller owns the trusted
contract and evidence root; this helper is not a signature service or sandbox.
Command receipts must come from the trusted task_run/test_run execution path.
They bind command/cwd, finite start/end times, process and wrapper exit codes,
cleanup outcome and full log hash. Old or interrupted receipts lacking terminal
wrapper fields remain unknown; preserve them without retroactive rewriting.
A later successful command never clears an earlier required failure.

UI/receiver/human claims require an explicitly supplied host adapter via
`verify(contract, evidence_root, event_verifiers=...)`. It returns VerifiedEvent
only after inspecting the authoritative source, including operation identity,
target, version, environment, mechanism and origin. Contract JSON cannot import
code. The CLI has no event adapter and therefore cannot certify real-user events.
A synthetic adapter is test evidence only; declaring origin=real_user does not
authenticate a user. Project adapters must verify the actual server/native record.
For UI entry/action, click or keyboard is supported; the exact requested mechanism
must match. GET and direct API substitution fail. Cross-operation/other-button
records also fail. Missing, malformed, tampered or broken-adapter evidence stays
unknown. No project UI adapter is supplied by this generic change.

The earlier local task_contract_advisory helper, if present, remains a legacy
trusted-record schema check. It does not authenticate sources and must not be
used to promote synthetic labels into actual-user acceptance. Existing files
and prior failure evidence are preserved.

## Runtime consistency

Process capacity is defined once in scripts/runtime_settings.py and copied with
the runtime; it is separate from agent/native/goal budgets. Doctor --diff compares
both skills and runtime. It reports raw hashes, treats only CRLF/LF differences as
equivalent, and still rejects real content drift.

`install_maintenance.py --runtime-only` previews runtime updates without reading
or editing hooks, config, schedules, global rules or local storage settings.
`--apply` saves backups and uses the existing raw-hash ownership checks.
The runtime-only preview returns `plan_sha256`. After reviewing that concrete
plan, supply `--expected-plan-sha256 <SHA256>` with `--apply`; source bytes,
target bytes, target home, migration pins and ownership state must still match.
A reviewed local modification may be migrated with --expected-current pointing
to an explicit JSON map of filename to current raw SHA-256. A stale pin fails;
this is not permission to overwrite arbitrary user files. Inspect the preview,
then apply the same verified source/target plan. A partial or conflicting update
retains backup and failure evidence; it is not a successful deployment.

## Small tests and interpretation

tests/test_generic_completion_deployment.py exercises actual tiny subprocesses,
failure followed by success, terminal metadata, hash tampering, synthetic UI and
handoff boundaries, runtime-only isolation, local edit conflicts and newline
handling. Existing workload tests exercise pool capacity and lease release.
These establish deterministic mechanics, not model compliance, scientific quality,
real-user UI acceptance or a project end-to-end pass.

The global template routes to the checker at meaningful boundaries. Existing
sessions must explicitly re-read changed rules/skills; a file update alone does
not prove a future session loaded them. Project integration comes after generic
deployment verification, using the original requested entry/action/outcome.
