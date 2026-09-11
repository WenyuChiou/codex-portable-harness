# Codex Portable Harness

A private, host-neutral adapter for Codex response profiles, bounded subagents,
memory proposals, and migration checks. Policy and checkpoint decisions come
from the released public `agent-collab-harness` contract; this repository does
not maintain a second policy evaluator.

Requires Python 3.10 or newer and the SHA-256-pinned `agent-collab-harness`
v0.5.1 release wheel. Both packages' runtime code uses only the standard
library.

The repository lives under `~/.codex/portable-harness`, but it is independent
from Codex runtime state. It never carries credentials, sessions, databases,
caches, machine identities, or host-local paths.

## Commands

```text
python scripts/bootstrap.py --dry-run
python scripts/bootstrap.py --apply
python scripts/doctor.py --cross-tool --diff
python scripts/policy_engine.py evaluate checkpoint.json  # public decision via thin legacy adapter
python scripts/policy_engine.py migrate old.json --metadata request.json --output new.json
python scripts/policy_engine.py advance new.json --request-id next-slice --expected-sha256 FILE_HASH
python scripts/route_execution.py select --task-class exploration
python scripts/memory_proposal.py propose --task-id TASK --summary TEXT --evidence REF
```

`bootstrap.py` deploys the two allowlisted skills and three standalone Codex
agents (`portable-mechanical`, `portable-explorer`, `portable-worker`) into the
active `CODEX_HOME`. It preserves custom agents and refuses unknown/user-modified
collisions. A host-local ownership manifest and exact old-deployment hashes
support repeatable upgrades; each file is published atomically. A failed batch
may be partially applied and must be inspected before re-running. No global
model, service tier, sandbox, or approval config is changed. Cross-tool exports
remain proposals inside this repository.
The legacy nested-metrics checkpoint remains parse-only for migration. New
checkpoints use the public `TaskCheckpoint` shape. Legacy unsigned overrides
fail closed. Ordinary v2 slice continuation does not require an override.

## Autonomous goals and model rollout

The canonical policy embeds the compatible v1 limits once. V1 checkpoints keep
their original stopping behavior; explicit migration creates v2 state without
rewriting the original. Metadata is JSON containing `goal_id` and `next_step`.
V2 retains cumulative usage and returns action/slice/goal scope. An eligible
slice advances automatically; cancellation, explicit total budgets and genuine
human gates remain hard. Codex, not this adapter, owns execution and approvals.

Context-only gates request local compaction (checkpoint, scope=action), not
human approval. Preserve source evidence and failures, measure a smaller active
packet, then re-evaluate before execution. Pending context maintenance cannot
be bypassed with slice advance. Explicit legacy migration recognizes only exact
slice/context budget reasons; ambiguous/human blockers stay stopped. Corrupt
inputs remain fail closed while the primary agent diagnoses them read-only.
Neither maintenance nor waiting alone is a reason to ask the user to continue.
Synthetic replay tests do not establish a real-task human-intervention rate.

Model mapping is defined only in `policies/model-routing.json`; agent files must
match it. Planning permits read-only organization and exploration, while
implementation and state changes remain blocked. Classify task effects separately
with `--task-effect`; read-only mechanical output stays in the response. Governance stays with the
parent; independent review uses another context. Daily task-class routing is enabled
by user preference without benchmark admission. Evidence remains necessary for
measured quality or efficiency claims. Explicit experimental
selection is evaluation-only and cannot authorize normal dispatch. Unknown cost
is null, not zero. No speed/cost improvement is claimed by this release.

The user-selected primary preference and explicit quality escalation complement
daily routing. An evidence-bound difficult task can select the configured
escalation model while ordinary bounded tasks use their configured task-class model.
The router does not switch the host model, provide computer-use access, or authorize
execution. See [model routing](docs/MODEL_ROUTING.md) for triggers and usage.

`benchmark_autonomy.py` currently supports offline evaluator testing only. It
cannot measure the native host's full planning/delegation/review lifecycle and
therefore refuses live runs and cannot emit promotable evidence. Its fixture
tests do not substitute for live paired evidence of measured savings or quality.
Daily routing is an explicit user preference, not a promotion of those results.

Doctor checks the active CLI against the tested version baseline, but a version
check is not a native model canary. CLI 0.144.1 was rejected for `gpt-6-astra`;
the same-model read-only routing canary passed on 0.153.3. Hosts with older or
unavailable CLIs receive a warning and must verify their own model compatibility.
`doctor.py --codex EXECUTABLE` can inspect a side-by-side CLI without changing
the global model, tier, sandbox, approval settings, or PATH.

Standalone-agent fields follow the
[official Codex subagent contract](https://learn.chatgpt.com/docs/agent-configuration/subagents).
New sessions load deployed definitions; old sessions must recheck loaded policy
and skill versions before resuming. See the shared package's
[0.5 migration contract](https://github.com/WenyuChiou/agent-collab-skills/blob/v0.5.1/docs/migration-0.5.md)
for signed gates, scoped decisions and rollback. A v2 record must never be
downgraded to erase accepted work or cumulative usage.
