# HEARTBEAT.md — Agent Execution Runbook

## Standard Flow
1. Read your assignment.
2. Use `planning-with-files:plan` if the research covers 3+ distinct questions.
3. Read the project's CLAUDE.md and history.md to understand prior research and context.
4. Research systematically: gather sources, synthesize, draw conclusions.
5. Use `verification-before-completion` — check that claims are supported by sources.
6. Write findings to the project's `research/` directory. Commit with issue reference.
7. Output the ##SUMMARY## block.

## When You Hit an Error
- **Source unavailable / paywalled:** Document what you couldn't access and use
  alternative sources. Do not fabricate findings.
- **Conflicting information:** Document the conflict and assess source credibility.
  Present both perspectives if unresolvable.
- **Stuck after 2 attempts:** Use `episodic-memory:remembering-conversations` to find
  prior research on this topic. Then use `systematic-debugging` to diagnose the blocker.
- **SSH/network failure or credential prompt:** Output ##BLOCKED## immediately.
  Do not hang waiting for input.

## What "Blocked" Means
##BLOCKED## is NOT "I gave up." It means:
- You tried at least 2 distinct approaches
- You documented what you tried and why each failed
- You identified exactly what a human needs to do to unblock you

Vague ##BLOCKED## messages are a failure mode. Include full error text and attempted steps.
