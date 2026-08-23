# Repository Contract

- This repository may write only inside the active `CODEX_HOME` directory.
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
