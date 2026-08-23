# External Skill Source Review

Reviewed on 2026-08-23 before implementation.

| Source | Pinned commit | Review result |
|---|---|---|
| `o4f6bgpac3/concise` | `f9468b0fb29aac6501b21a289e58809b271783ed` | Low runtime risk; instruction-only skill. No declared license, so no text was copied. |
| `nathanksou/eli5` | `549364af799a4a0556c5359a0ac3e36d4da5719d` | Base ELI5 skill is instruction-only. No declared license, so no text was copied. |
| `mgifford/accessibility-skills` | `f2cbb99c5a45a02f47002923f020f5098b552bb0` | Plain-language guidance is instruction-only. No declared license, so no text was copied. |

`eli5-compress` was rejected. Its declared workflow overwrites a canonical
memory file, creates a backup beside it, invokes an external model CLI, and
retries mutations. Those permissions are unnecessary for response formatting.

This repository independently implements general ideas: lead with the outcome,
remove filler, explain unfamiliar terms once, and expand when brevity would hide
risk or evidence.
