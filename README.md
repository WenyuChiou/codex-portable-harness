# Codex Portable Harness

A private, host-neutral adapter for Codex response profiles, bounded subagents,
memory proposals, and migration checks. Policy and checkpoint decisions come
from the released public `agent-collab-harness` contract; this repository does
not maintain a second policy evaluator.

Requires Python 3.10 or newer and the SHA-256-pinned `agent-collab-harness`
v0.4.0 release wheel. Both packages' runtime code uses only the standard
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
python scripts/memory_proposal.py propose --task-id TASK --summary TEXT --evidence REF
```

`bootstrap.py` deploys only the two allowlisted skills into the active
`CODEX_HOME`. Cross-tool exports remain proposals inside this repository.
The legacy nested-metrics checkpoint remains parse-only for migration. New
checkpoints use the public `TaskCheckpoint` shape. Legacy unsigned overrides
fail closed and must be replaced by a signed public override before exhaustion.
