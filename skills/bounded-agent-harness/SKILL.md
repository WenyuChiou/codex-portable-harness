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

Stop and return control to the parent when the policy engine says `stop`.
Checkpoint and compact when it says `checkpoint`. A loop counts as progress only
when it adds a verifiable artifact, test result, source, decision, or blocker.

Budget overrides must be declared before exceeding a limit and include a reason.
Never silently retry, silently switch models, silently discard context, or treat
memory as more authoritative than current user instructions and repository state.

Memory output is proposal-only. Use `memory_proposal.py`; do not write, archive,
supersede, or delete canonical memory without an explicit recorded decision.
