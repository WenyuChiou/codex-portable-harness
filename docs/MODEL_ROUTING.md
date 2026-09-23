# Primary work and bounded quality escalation

The canonical model and effort values live in `policies/model-routing.json`.
`primary_preference` inherits the actual Codex session model. It never pins a
model or rewrites host configuration.

The primary model owns coordination and coherent small tasks. When delegation
is useful, automatically classify each bounded task and use the policy's route:
`mechanical` for stable testable transformations, `exploration` for read-only
investigation, and `implementation` for independent changes with clear acceptance.
Keep trivial work local; never create a child merely to use a different model.

Task class and effect are separate. `mechanical --task-effect read-only` can
organize supplied facts in Plan or execution mode: sort, group by explicit keys,
deduplicate exact repeats, or populate a fixed response template. Preserve source
references and flag omissions. New investigation uses exploration; priorities,
conflicting interpretations and substantive planning remain with the parent.
For example, grouping by a supplied priority field is mechanical; deciding that
priority is not. A small coherent organization task stays local.

```text
python scripts/route_execution.py select --mode plan --task-class mechanical --task-effect read-only --available-model <available-mechanical-model>
```

Read-only mechanical work selects `portable-mechanical-readonly`, which has a
native read-only sandbox. Escalated read-only exploration selects the dedicated
`portable-quality-explorer` role, pinned to the policy's quality model and a
read-only sandbox. The host must also copy
`task_effect` and `read_only_required` into the focused packet and enforce
response-only output with no writable paths or mutating tools. Routing metadata
alone is not a sandbox configuration.
File edits, UI changes, API mutations and other state changes use `write` and
remain blocked in Plan. Mechanical calls omitting the effect conservatively
retain their old write classification. Implementation cannot be relabeled read-only
to bypass Plan; conflicting classes/effects require reclassification. Permission,
model availability, checkpoint and experimental-only guards remain unchanged.

Daily routing is enabled by user preference. The policy explicitly sets
`economical_routing_require_attestation` to false, so missing benchmark evidence
does not block daily delegation. This is not measured proof of savings or quality.
Setting it to true restores strict evidence admission; older policies missing the
field retain that requirement. Disabling rollout retains primary execution.
Returned `attestation_valid` reports actual evidence validity independently.
`--experimental` remains evaluation-only even when daily routing is enabled.

Call the router with task class and actual available models. No per-task user
model selection is required. A selected model unavailable on the host requires
an explicit parent decision, not silent substitution or repeated failing attempts.
Model changes apply to separate bounded children, not in-place switching of the
main conversation or a running child. Reuse the checkpoint and accepted evidence.

Apply this allocation throughout a single task, including during and after Plan.
For each future write subtask, the plan records its provisional class, effect,
role, dependencies, deliverable and acceptance check, **not** a fixed model. A future
mechanical edit can be assigned in the plan while its execution remains deferred.
Use the actual Plan mode for live dispatch; read the canonical map to plan future
assignments, never pass execution mode merely to evade a Plan restriction.
Intended assignments do not establish dispatch readiness. Missing inputs or an
escalation's future evidence remain pending until they actually exist.

When execution begins or a task resumes in a new session, reselect the model and
effort against current policy, availability, permissions and local attestation.
Do not re-run accepted work with unchanged relevant inputs or switch a running
child midway. Re-evaluate only affected task boundaries when scope, difficulty,
dependencies or model availability change. Straightforward mechanical steps remain eligible for their route even
though the parent created the plan. Missing facts go to investigation; substantive
decisions return to the parent or a justified escalation. Do not exhaust retries
on the wrong task class. The parent integrates and accepts the result.

## Candidate role promotion

Baseline daily roles remain active without promotion evidence. Candidate roles
defined in the same policy are **not** active merely because a newer model exists.
On each host, run `python scripts/benchmark_role_promotion.py --task-class CLASS`
for `mechanical`, `exploration`, or `implementation`. Each role uses eight paired
measurements per role, randomized within-pair order, two excluded warmups,
identical service tier and byte-identical fixture starts. Mechanical and
exploration roles repeat two distinct fixtures four times; implementation uses
four fixtures twice. Each fixture is an isolated temporary Git repo under the
active Codex home. The verifier is trusted static/JSON code outside the agent
fixture and never executes model-edited Python. All candidate answers must pass;
control quality failures are recorded and may be rescued, but both arms still
need complete usage and no sandbox violation. Candidate failures/retries/timeouts
cannot increase, and median end-to-end model-call wall time and comparable token-priced
API cost proxy must each stay within 15% of baseline. The proxy is **not** an
actual ChatGPT subscription bill or proof of cheaper total project cost.
Both arms disable Python bytecode writes to avoid sandbox-owned cache files;
an unreadable fixture entry fails integrity verification instead of being skipped.

Passing creates a 30-day host-local attestation, tied to a hash of a local salt
and independent OS machine identity, OS, Python and Codex versions, active tier,
policy, complete benchmark/verifier source hash, raw evidence hash and current
price snapshot. Suite text is hashed with LF-normalized line endings so a clean
Git checkout on a CRLF host does not invalidate equivalent benchmark code;
substantive source changes still invalidate it. Copying CODEX_HOME alone does not
transfer admission. Bootstrap
then deploys only the admitted candidate agent definitions and revokes exact,
unchanged, manifest-owned candidate files when admission expires. The router requires
that exact deployment plus current model availability. Missing, expired or
mismatched evidence, unsupported models, or undeployed roles retain the baseline;
permission refusal still blocks execution. `doctor --diff` reports deployment
drift and promotion admission. Raw JSON events, host ID and attestations stay
under the local Codex home; never commit them. The benchmark does not change the
primary model, service tier or approval policy. A newer candidate requires its
own policy update and a fresh per-host eight-pair evaluation.

### Anonymous local evaluation snapshot (2026-09-23 UTC)

One host ran role evaluations with identical service tier and fixtures per
pair. Mechanical and implementation failed quality before the line-ending hash
correction; exploration was rerun with the corrected hash. These results are
host-specific, not a cross-platform or real-billing claim:

| Role | Candidate quality | Gate | Median wall change | API price-proxy change |
|---|---:|---|---:|---:|
| Mechanical | Failed on formal call 12/16 | Not admitted; baseline retained | Not calculated | Not calculated |
| Exploration | 8/8 pairs passed; no extra retries, timeouts or sandbox violations | Not admitted; cost regression exceeded 15% | +11.4% | +16.0% |
| Implementation | Failed on formal call 10/16 | Not admitted; baseline retained | Not calculated | Not calculated |

The exploration run was repeated after the line-ending hash correction because
the earlier attestation used the nonportable suite hash. Its new API cost proxy
exceeded the approved ceiling, so **no candidate role is active on this host**.
Failed roles are not retested merely to seek a lucky pass; they require an
evidence-backed change and a fresh complete evaluation. Other hosts start from
the baseline and must produce their own valid attestation before using any
candidate.

For example, a feature task may use read-only exploration to locate interfaces,
mechanical organization to tabulate the findings, parent judgment to choose the
design, then mechanical edits and independent implementation after Plan. A hard
architectural question or important review may justify escalation. No task needs
all models, and a small coherent step can remain inline.

Use a stronger child only for a concrete independent task:

- `research_methodology`: conflicting assumptions, measurement validity or hard derivation.
- `architecture_tradeoff`: consequential cross-system choices with competing constraints.
- `complex_debugging`: evidence-backed multi-component ambiguity beyond routine diagnosis.
- `high_risk_review`: an independent review of a stable important candidate.
- `complex_computer_use`: difficult visual interpretation or multi-step UI state.

These are routing preferences, not performance benchmarks. Routine browser use
alone is not a trigger. A repository requiring stronger independence or review
still takes precedence. Governance/security ownership and final judgment remain
with the parent; advice can use a scoped review task.

```text
python scripts/route_execution.py select --task-class review --parent-model <actual-parent> --escalation-reason high_risk_review --evidence-ref <stable-diff-reference> --available-model <available-escalation-model>
```

For computer use, also pass `--computer-use-available` only after checking the
child's tools and intended surface. Read-only UI inspection uses `exploration`;
state-changing interaction uses `implementation` and is not allowed in Plan mode.
Prefer a suitable API/CLI, supply observed tab/window references, and assign one
operator to a shared surface. If tool/surface access cannot be transferred,
retain execution with the capable parent and delegate read-only advice only.

`dispatch` selects a model; the host must separately evaluate the checkpoint,
permissions, task scope, budget and any action approval. A denial never permits
switching models. `parent_decides` is explicit unavailability or missing routing
evidence, not permission to silently try another model. A parent already using
the escalation model needs no extra escalation; required review still uses a
separate context. `--experimental` remains evaluation-only.

Use one focused packet with relevant facts, exact ownership, expected evidence
and a stop condition. Read the current checkpoint and its referenced on-disk
evidence first; stale summaries must not reopen approved decisions or completed work.
For native `spawn_agent`, pass the selected model/effort with `fork_turns="none"`
and the focused packet, or a bounded positive history count. Full-history forks
inherit the parent and cannot apply those overrides.
Resume the primary workflow after delivery. Reuse accepted
evidence when its inputs remain unchanged; do not launch redundant agents or
re-read whole histories merely to populate a packet. The managed agent TOMLs
mirror the policy's model and effort defaults so native agents remain usable;
doctor checks that they agree. Do not duplicate routing values in prose or
unrelated host configuration.
