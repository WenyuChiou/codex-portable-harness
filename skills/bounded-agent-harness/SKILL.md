---
name: bounded-agent-harness
description: Apply portable budgets, checkpoints, and evidence-based stopping rules when coordinating subagents, retries, long-running tool loops, or resumable agent tasks.
---

# Bounded Agent Harness

Use the canonical policy at
`${CODEX_HOME}/portable-harness/policies/agent-budget.yaml`. If `CODEX_HOME` is
unset, resolve it as `~/.codex`.

Before spawning work, give every child a parent ID, bounded task, expected
evidence, result shape, and stop condition. Pass a focused task packet rather
than full history unless full context is essential and explicitly justified.

Before every spawn and after every plan–act–reflect cycle, update a
`TaskCheckpoint` with evidence references, child counts, context sizes, and
measurable counters. Run the thin `policy_engine.py evaluate` adapter, which
delegates to the released `agent-collab-harness` public contract; do not spawn when its
`spawn_allowed` field is false.

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
exploration, not implementation. Governance/security/final judgments stay with
the parent; required reviewers use a separate context and inherit its model.
Economical routes require validated host-local evidence and an enabled rollout;
experimental routing is for explicit evaluation only. Preserve service tier.
Log model, effort, reason, expected evidence, and unavailable-model decisions.
Never switch models to bypass a platform or permission refusal.

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
