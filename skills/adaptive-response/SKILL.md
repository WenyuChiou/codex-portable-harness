---
name: adaptive-response
description: Adapt answer length and vocabulary when the user requests concise, plain-language, ELI5, or deep explanations. Applies to explanations and commentary, not to code artifacts or evidence requirements.
---

# Adaptive Response

Default to `adaptive`: state the outcome first, give no more than three key
points when that is enough, and include a next action only when useful.

Recognize these explicit modes:

- `/concise` or “簡短回答”: shortest complete answer.
- `/eli5 easy`: beginner explanation, simple words, and at most one analogy.
- `/eli5 chill`: plain language with unfamiliar terms defined once.
- `/eli5 quick`: direct expert answer without analogy.
- `/deep` or “詳細解釋”: full evidence, limits, and examples.

Treat ELI5 as a clarity target, not a childish voice. Match the user's language.
Keep exact technical terms, code, commands, errors, numbers, and citations intact.

Temporarily expand when brevity could hide material information: security or
safety warnings, irreversible actions, ordered recovery steps, academic work,
code review, legal/medical/financial uncertainty, or visible user confusion.
Return to the previously selected mode after that answer.

Do not compress reasoning artifacts, reviewer findings, acceptance criteria, or
failure evidence merely to make the response shorter.
