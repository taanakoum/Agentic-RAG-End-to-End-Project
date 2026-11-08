---
name: rag-checkpoint-handoff
description: Safely stop or pause work (usage limit at 90%, context exhaustion, blockers) by persisting state, updating control docs, committing, and writing an exact handoff. Use when weekly usage >= 90% or when ending a session.
---

# Checkpoint and handoff

1. Stop starting new work. Finish only an atomic step that can be left consistent; otherwise stash the partial change on the feature branch in a clearly named WIP commit and say so.
2. Update `docs/IMPLEMENTATION_STATUS.md`: statuses (nothing incomplete marked done), current phase, last usage reading, **Next step** as an exact command or task.
3. Append to `docs/EXECUTION_LOG.md`: what was done, files, tests run + real results, open defects, remaining work.
4. Update `docs/KNOWN_ISSUES.md` and `docs/ARCHITECTURE_DECISIONS.md` for anything new.
5. Make sure the repo imports and that failing tests are recorded, not hidden. `git add -A && git commit` with a descriptive message on the feature branch.
6. Final message to the user (short): phase/state, what is verified vs not, exact next step, usage level, and a reminder that nothing continues running after this session.
