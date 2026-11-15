# Pre-commit review rules

Read this file before every commit. This is the detailed review route referenced
by the global AGENTS.md template; project requirements retain precedence.

Run exactly one general review on the final stable staged diff if any applies:

| Trigger | General reviewer |
|---|---|
| At least 50 changed lines OR at least 3 files | code-review for size alone |
| Release, version or tag | independent code-reviewer |
| Auth, error handling, file I/O, network or security path | independent code-reviewer |
| Codex or Gemini delegate returned shipping output | independent code-reviewer |
| AGENTS.md, CLAUDE.md, CONTRIBUTING.md, hooks, permissions or environment governance | independent code-reviewer |
| User explicitly requested independent review | independent code-reviewer |

Governance/security/release/delegation/explicit review takes precedence over the
size-only route. Never run both general reviewers for the same fingerprint.
Record reviewer, trigger and verdict in the commit.

Only a one-line typo/format/comment, pure non-executable allowlist addition,
doc-only change below 50 lines AND 3 files without executable blocks, revert,
or explicit user-recorded skip may bypass review. An exception to one trigger
does not clear another applicable trigger.

Specialists are additive and bounded:
- silent-failure-hunter-py: final research-critical production Python diff only,
  after a concrete silent-failure signal.
- harness-drift-monitor: explicit audit or material routing, permissions, hook,
  agent, skill or global-governance change.
- banned-word-auditor: once for a substantial candidate manuscript section.

Invoke once per role and stable fingerprint, never overlapping retries. After
relevant fixes request only the affected recheck. Start with local preflight;
repo size, time spent or file count alone does not justify a specialist.
