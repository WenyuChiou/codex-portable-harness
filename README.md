# Codex Portable Harness

A private, host-neutral control layer for Codex response profiles, bounded
subagents, memory proposals, and migration checks.

Requires Python 3.10 or newer. Runtime tools use only the standard library.

The repository lives under `~/.codex/portable-harness`, but it is independent
from Codex runtime state. It never carries credentials, sessions, databases,
caches, machine identities, or host-local paths.

## Commands

```text
python scripts/bootstrap.py --dry-run
python scripts/bootstrap.py --apply
python scripts/doctor.py --cross-tool --diff
python scripts/policy_engine.py evaluate checkpoint.json
python scripts/memory_proposal.py propose --task-id TASK --summary TEXT --evidence REF
```

`bootstrap.py` deploys only the two allowlisted skills into the active
`CODEX_HOME`. Cross-tool exports remain proposals inside this repository.
