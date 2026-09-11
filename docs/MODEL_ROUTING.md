# Primary work and bounded quality escalation

The canonical model and effort values live in `policies/model-routing.json`.
`primary_preference` records the user's normal session preference. It does not
rewrite Codex configuration or change an already running parent.

Keep ordinary implementation, lookup, testing, CI, metadata and delivery with
the primary model. Use a stronger child only for a concrete independent task:

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
re-read whole histories merely to populate a packet. Model names, efforts and
rollout settings must not be duplicated in agent-specific defaults.
