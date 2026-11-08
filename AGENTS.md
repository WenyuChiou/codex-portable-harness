# Repository Contract

- Before repository work run `git rev-parse --show-toplevel`. If it is the account
  home, stop there and report; continue only in a verified project checkout.
- This repository may write only inside the active `CODEX_HOME` directory.
- Storage maintenance may remove explicitly configured legacy Codex task paths
  after a preview, user approval and an unchanged reviewed plan. Keep shared or
  unowned data, protected product homes, backup roots and Ollama models. The home
  repository's Git history is proposal-only until independently backed up.
- New scratch belongs under `CODEX_HOME/tmp`; new clones and linked worktrees
  belong under `CODEX_HOME/worktrees`. On task completion or abandonment, clear
  owned disposable scratch and report retained paths with their reasons.
- Run autonomous work through `task_run.py` (three executing jobs maximum) and
  tests through `test_run.py` (one heavy suite). Preserve native host controls.
- Before task completion update the project's existing handoff, or its
  `CODEX_HOME/maintenance/handovers/<project>.md`: progress, output locations,
  still-needed worktrees, and the next step. Keep host-local details out of commits.
- Codex worktree maintenance may update preservation refs and the registered
  worktree metadata in a candidate's owning Git repository. This exception is
  limited to clean, idle linked worktrees below `CODEX_HOME/worktrees`.
- Maintenance has one explicit ACL-only exception: a user-selected path or a
  task-end hook's generated test directories below the current user's
  `Documents/Codex`. Back up ACLs inside `CODEX_HOME`, retain other principals,
  grant only the current user, and restore sandbox-owned directories to that
  user. This exception authorizes no file deletion or Git trust exemptions.
- Explicitly invoking `repair_windows_python.ps1 -Apply` permits one narrow
  exception: back up ACLs inside `CODEX_HOME`, then add inheritable read/execute
  for the existing `CodexSandboxUsers` group on the virtual environment's
  verified base-Python installation only. Require a reproduced Windows error 5;
  never grant write access or change sandbox/network/approval settings.
- Never read from or write to another agent product's home directory.
- Never commit credentials, OAuth data, session transcripts, databases, caches,
  machine identities, host-local manifests, or absolute user paths.
- Cross-tool files under `exports/` are proposals only. Do not apply them.
- Memory lifecycle commands create or decide proposals only. They never delete
  or rewrite canonical memory.
- Use Python standard-library dependencies only.
- Before committing, run `python -m unittest discover -s tests -v` and
  `python scripts/doctor.py --cross-tool --diff`.
- Stage explicit paths and verify the staged file count. Never use `git add -A`.

## Code Review Rules

These rules apply only while conducting code review. They do not replace
implementation or publication instructions or valid authorization for other tasks.

- Review the actual diff at its stated base and head, then trace only the
  affected callers, consumers, schemas, policy/configuration, and CI path.
  Give a concrete file/line or symbol, trigger, consequence, and evidence.
- Separate confirmed defects from hypotheses and verification gaps. Do not
  report style preferences as defects. Mocked behavior, fixture markers, and
  unrun live experiments are not completed results or measured quality.
- Check that checkpoints, approvals, accepted results, cumulative usage, and
  failure history survive resume, slice transitions, migration, and retries.
  Treat lost, duplicated, or reset state as a defect when the affected contract
  requires it to persist.
- For wait, cancel, timeout, retry, and child/slot lifecycles, check
  idempotency, terminal-state handling, duplicate side effects, capacity or
  lease release, and the authorization boundary. A retry needs a relevant
  input or external-state change.
- For routing and Plan behavior, inspect the active router map and decision /
  task-effect gates. A fallback, model change, or reclassification must not
  bypass a permission denial or platform refusal; Plan remains read-only.
- For usage and quality claims, follow the provider's accounting contract:
  cached input is not billed twice, and unknown cost or token data stays
  unknown rather than zero. Offline proxies and fixtures support deterministic
  mechanics only; they do not establish live quality or savings.
- For implicit skill activation, require runtime evidence that the selected
  skill actually activated and affected the task; selection metadata alone is
  insufficient. For bootstrap and doctor paths, check active `CODEX_HOME`
  resolution, drift and collision detection, and rollback protection without
  assuming a fixed user path.
- Use the existing `tests/` and CI evidence selected by the changed path.
  State uncovered behavior as a verification gap; do not add a second process
  manager or describe an unrun experiment as evidence.
- Bind conclusions to the exact base/head, changed paths, applicable policy or
  rubric versions, and evidence hashes. Follow the platform's required finding
  format and location conventions.
- A review task itself does not modify code, install dependencies, start paid
  models, or merge. Every PR merge needs separate explicit user consent after
  the user receives the review summary, actual test status, and remaining gaps.
