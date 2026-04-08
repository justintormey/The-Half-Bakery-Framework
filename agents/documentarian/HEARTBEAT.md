# HEARTBEAT.md — Agent Execution Runbook

## Standard Flow
1. Read your assignment.
2. Use `planning-with-files:plan` if the documentation spans 3+ distinct areas.
3. Read the project's CLAUDE.md and history.md to understand what already exists.
4. Write documentation that captures decisions and rationale, not just events.
5. Use `verification-before-completion` — ensure all referenced files/versions are accurate.
6. Commit with messages referencing the issue number.
7. Output the ##SUMMARY## block.

## When You Hit an Error
- **File path not found:** Search for the correct path (`find`, `ls`) before giving up.
- **Version number unclear:** Check git tags (`git tag --sort=-v:refname`) and CHANGELOG.
  Do not guess — leave a TODO if genuinely ambiguous.
- **Stuck after 2 attempts:** Use `episodic-memory:remembering-conversations` to find
  prior documentation patterns. Then use `systematic-debugging` to diagnose the blocker.
- **SSH/network failure or credential prompt:** Output ##BLOCKED## immediately.
  Do not hang waiting for input.

## What "Blocked" Means
##BLOCKED## is NOT "I gave up." It means:
- You tried at least 2 distinct approaches
- You documented what you tried and why each failed
- You identified exactly what a human needs to do to unblock you

Vague ##BLOCKED## messages are a failure mode. Include full error text and attempted steps.
