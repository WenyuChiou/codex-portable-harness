---
name: bounded-agent-harness
description: Apply portable budgets, checkpoints, and evidence-based stopping rules when coordinating subagents, retries, long-running tool loops, resumable tasks, or running local tests with disposable scratch.
---

# Bounded Agent Harness

For local test commands, use the installed `maintenance/runtime/test_run.py`
under the active Codex home with an explicit `--timeout SECONDS`. Use the
project's Python to run `test_run.py --timeout SECONDS --pytest -- PYTEST_ARGS`,
or `test_run.py --timeout SECONDS -- COMMAND ...` for other test runners. Put
disposable script outputs in `CODEX_TEST_SCRATCH`; keep deliverables in their
intended location. Success removes owned scratch, failure retains it seven
days, and the nightly task handles abandoned runs. This route adds no budget
checkpoint or orchestration loop for a fixed native test command. If the
entrypoint is unavailable, report that gap and use an explicitly owned temp
directory with a foreground timeout and task-end cleanup.

Use the canonical policy at
`${CODEX_HOME}/portable-harness/policies/agent-budget.yaml`. If `CODEX_HOME` is
unset, resolve it as `~/.codex`.

Activate this harness for agent delegation, open-ended agent loops, and governed
recovery. A previously authorized, fixed serial batch with its own durable
request budget and failure journal does not need a second orchestration loop
merely because its native process runs for a long time. Keep an already adopted
goal's counters and evidence; do not disable a live hard limit or reset state.

Before spawning work, give every child a parent ID, bounded task, expected
evidence, result shape, and stop condition. Pass a focused task packet rather
than full history unless full context is essential and explicitly justified.

Before every spawn and after each substantive plan–act–reflect cycle, update a
`TaskCheckpoint` with evidence references, child counts, context sizes, and
measurable counters. Run the thin `policy_engine.py evaluate` adapter, which
delegates to the released `agent-collab-harness` public contract; do not spawn when its
`spawn_allowed` field is false.

An unchanged status poll, receipt lookup, or healthy scheduler wake is not a new
cycle. Accumulate the real tool/time observations and flush them at the next
execution, delegation, recovery, scope change, or budget boundary. Evaluate
before further governed actions when a limit is reached. Await completion of a
checkpoint write before starting a reader or dependent transition. Use
`evaluate --compact` when only a decision summary is needed; it changes output,
not evaluation or limits. Keep bulky raw evidence outside the live checkpoint,
referenced by path/hash, and never discard accepted history to shrink it.

Resource snapshots, repeated connection probes, short-lived readiness grants,
and per-seed approvals are not requirements of this harness. Add such a gate
only for a concrete applicable contract or observed contention, with the reason
and minimum scope recorded. Do not invent another quota after the same failing
preflight exhausts one. Diagnose the dependency and reuse unchanged accepted
preparation. Honor a direct human waiver of a discretionary project check with
an honest authority record; never fabricate measured-idle/sample evidence or
use it to bypass platform permissions, signed authorization, or hard budgets.
Routine stage progression does not require another review of unchanged code.
See `docs/LOW_OVERHEAD_OPERATION.md` in the portable adapter repository.

For v2, obey decision scope. `stop` with scope=action stops that action, not
safe primary-agent diagnosis. A goal stop preserves cancellation, explicit hard
budgets, or an authorization gate. `checkpoint` with scope=slice and
auto_continue=true means save and call the adapter's `advance`, then continue
the same authorized objective without asking for a human override. Other
checkpoint decisions require evidence or compaction before re-evaluation.

For context_compaction_required, perform safe local maintenance without asking
the user to renew authorization. Preserve full source artifacts, accepted
evidence, unresolved failures, and human decisions; create a smaller linked
packet or use native compaction. Record measured active context sizes and
re-evaluate before execution/spawn. Do not merely zero observations or advance
a slice to bypass context limits. Cumulative usage and failure history persist.
If policy/checkpoint parsing fails, stop governed execution, diagnose read-only,
and recover only from verified state; never invent authorization or clear an
ambiguous legacy blocker. Escalate only when no safe in-scope next step remains.

Do not end a task merely to ask "continue?" after routine maintenance, a review
correction, or an eligible slice boundary. A scoped action stop does not stop
independent safe work. Waiting uses the native wait/monitor mechanism, not a
busy retry loop. Record real human interventions separately from automatic
recovery and external waiting; report measured rates, not synthetic success as
live-task evidence. Native limits and genuine authorization gates still win.

The shared package owns accounting, not execution. Codex owns the goal loop,
subagents, sandbox, and approvals. Never start a separate process manager or
background schedule merely because this skill is active.

Existing v1 checkpoints retain their old behavior. Explicitly migrate to a new
file with `policy_engine.py migrate OLD --metadata REQUEST --output NEW`.
REQUEST contains goal_id and next_step as JSON. Keep OLD read-only. Use
`policy_engine.py advance PATH --request-id ID --expected-sha256 HASH` for an
eligible v2 slice; reuse ID after uncertain completion. HASH covers file bytes.
Read the installed shared package's v2 contract when constructing checkpoints.

Track absolute slice metrics plus cumulative totals, accepted evidence, failure
history, and next step. Never reset totals on a session, model, or slice change.
Use actual observations; unavailable cost/tokens are unknown, not zero. Honor
the stricter explicit user/native limit without inventing a new total cap.
Completion requires acceptance evidence, not budget exhaustion or tool failure.

Before useful independent delegation, call
`route_execution.py select --task-class CLASS` with current model availability.
Trivial/coherent work stays with the parent. Planning permits read-only
organization and exploration, not implementation. Governance/security/final judgments stay with
the parent; required reviewers use a separate context and normally inherit its model.
Daily task routing follows the enabled canonical policy without requiring a
benchmark attestation. Classify each useful bounded task, then use the returned
role/model/effort; do not ask the user to pick a model for routine work. Optional
strict attestation mode remains available for evidence-gated dispatch.
Experimental routing is evaluation-only. Preserve service tier and execution gates.
Log model, effort, reason, expected evidence, and unavailable-model decisions.
Never switch models to bypass a platform or permission refusal.

Classify effects separately: pass `--task-effect read-only` for mechanical
organization of supplied facts, including in Plan; use `write` for any file, UI,
API or other state change. Omitted mechanical effects conservatively imply writes.
Carry returned `task_effect` and `read_only_required` into the child packet.
Read-only mechanical output is response-only, with no writable paths or mutating
tools; the host enforces these constraints. Routing metadata does not install a
sandbox. Preserve sources and mark missing facts; new investigation uses exploration,
while priorities and substantive synthesis stay with the parent. Small coherent
organization stays local when delegation costs more than doing it directly.

Route across the whole task lifecycle, not only the current mode. In Plan, save
each future write subtask's provisional class, effect, role, dependencies,
deliverable and acceptance check; do not pin a future model or dispatch it.
The router returns `plan_only` and `reselect_on_execution` for these tasks.
Read-only Plan work may dispatch under normal gates. At execution or a later
session boundary, call the router again with current model availability,
permissions, policy and host-local promotion evidence. An already running child
keeps its model until it finishes. Reuse accepted results whose relevant inputs
are unchanged; do not repeat them merely because a model or session changed.
Review correction and eligible slice advance preserve cumulative usage and
authorization. Reclassify a task needing more investigation or judgment rather
than repeatedly retrying a mismatched worker.

The policy's `primary_preference` inherits the active parent session; it does not
change the host's current model. Candidate role models require a valid local
eight-pair attestation, current policy and suite hashes, matching runtime, an
available model, and a managed deployed agent. Missing or stale proof keeps the
baseline route; it does not interrupt an authorized goal or trigger blind model
retries. A CLI/model refusal is a concrete unavailable-model signal: diagnose
once and use a safe available route at the next child boundary. Never switch a
running child or bypass permission denial. Explicit `quality_escalation` is
separate from daily task routing and makes no measured cost or quality claim.
For a difficult bounded task, call the router with the actual `--parent-model`,
an allowed `--escalation-reason`, and `--evidence-ref` for the concrete difficulty.
Use the returned model/effort only when action=dispatch and the checkpoint also
allows spawning. Missing evidence, tools or model availability requires a parent
decision, not a silent fallback. A required review may use the escalation model;
the parent retains scope, permissions, implementation ownership and final judgment.

Do not escalate routine reads, CI polling, straightforward edits, or ordinary
browser operations. Complex computer use may qualify when visual ambiguity or
multi-step UI state needs additional judgment; assert `--computer-use-available`
only after confirming the child can access the needed tools and intended surface.
Prefer APIs/CLIs when suitable. Transfer exact tab/window references and visible
state, assign one operator per surface, and preserve all action approval rules.
If the surface cannot be delegated, keep execution with the capable parent and
delegate only a bounded read-only assessment. Model choice does not grant tools.

Give an escalation a short focused packet, evidence and a stop condition. Return
to the primary workflow after its result; do not escalate all later tasks, repeat
unchanged attempts, or fan out merely because a stronger model is available.
Read the current checkpoint and its on-disk evidence before composing the packet;
stale summaries must not duplicate completed work or reopen approved decisions.
With native `spawn_agent`, apply returned model/effort using `fork_turns="none"`
and the focused packet, or a bounded positive history count. Full-history forks
inherit the parent and cannot apply model/effort overrides.

Use the smaller native/policy concurrency cap, release active capacity when a
child finishes, and reserve capacity for required review. Do not spawn just to
use multiple models. Agent boundaries alone do not require commits. Reuse tests
and review evidence only when their relevant inputs and requirements match.

True budget exceptions require the existing trusted-host authorization mechanism;
ordinary slice advances do not. Never mint a human signature as an agent.
Same-action retry exhaustion permits diagnosis, not blind repetition. Stable
failure identity covers operation, target, relevant inputs/version, and error
class. Only a evidenced correction or changed external state permits a bounded
new attempt, retaining prior failures. Waiting for CI is not a retry failure.

Work autonomously within the current authorization, including routine reversible
changes and authorized delivery. Preserve valid authorization across sessions.
Stop for concrete irreversible/high-impact actions outside that authorization,
material scope expansion, or a user decision that cannot safely be inferred.
Never silently retry, silently switch models, silently discard context, or treat
memory as more authoritative than current user instructions and repository state.

Memory output is proposal-only. Use `memory_proposal.py`; do not write, archive,
supersede, or delete canonical memory without an explicit recorded decision.
