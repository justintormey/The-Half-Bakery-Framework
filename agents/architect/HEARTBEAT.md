# HEARTBEAT.md — Agent Execution Runbook

## Standard Flow
1. Read your assignment.
2. Use `planning-with-files:plan` to structure a complex design problem.
3. Read the project's CLAUDE.md and history.md to understand existing architecture decisions.
4. Design systematically: problem → constraints → options → recommendation → migration sequence.
5. Use `verification-before-completion` — ensure the spec is internally consistent.
6. Write the design document to `docs/superpowers/specs/` or `docs/`. Commit with issue reference.
7. Output the ##SUMMARY## block.

## When You Hit an Error
- **Missing context / unclear requirements:** Document the ambiguity, state your assumption,
  and proceed. Mark assumptions clearly in the spec.
- **Conflicting constraints:** Surface the conflict explicitly in the spec. Propose a resolution.
- **Stuck after 2 attempts:** Use `episodic-memory:remembering-conversations` to find
  prior architectural decisions on this topic. Then use `systematic-debugging`.
- **SSH/network failure or credential prompt:** Output ##BLOCKED## immediately.
  Do not hang waiting for input.

## What "Blocked" Means
##BLOCKED## is NOT "I gave up." It means:
- You tried at least 2 distinct approaches
- You documented what you tried and why each failed
- You identified exactly what a human needs to do to unblock you

Vague ##BLOCKED## messages are a failure mode. Include full error text and attempted steps.
