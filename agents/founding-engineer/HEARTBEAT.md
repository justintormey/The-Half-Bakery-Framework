# HEARTBEAT.md — Agent Execution Runbook

## Standard Flow
1. Read your assignment.
2. Use `planning-with-files:plan` if the task has 3+ distinct steps.
3. Read the project's CLAUDE.md and history.md before touching code.
4. Do the work in focused, committed increments.
5. Use `verification-before-completion` before claiming done.
6. Commit with messages referencing the issue number.
7. Output the ##SUMMARY## block.

## When You Hit an Error
- **Git merge conflict:** `git status` to see conflicting files. Resolve by keeping the
  correct version, `git add`, `git commit`. Do not abort the merge.
- **Command not found / tool missing:** Document exactly what failed.
  Check if there's an alternative path (e.g., different binary name, pip install).
  Do not retry the identical command 5 times.
- **Unexpected file state:** Read what's there before overwriting. Use `git diff`.
- **Stuck after 2 attempts:** Use `episodic-memory:remembering-conversations` to search
  for how this was solved before. Then use `systematic-debugging` to diagnose root cause.
- **SSH/network failure or credential prompt:** Output ##BLOCKED## immediately with
  the exact error. Do not hang waiting for input that will never arrive.
- **Tests failing:** Use `systematic-debugging` — read the error, hypothesize, fix, verify.

## What "Blocked" Means
##BLOCKED## is NOT "I gave up." It means:
- You tried at least 2 distinct approaches
- You documented what you tried and why each failed
- You identified exactly what a human needs to do to unblock you

Vague ##BLOCKED## messages are a failure mode. Include full error text and attempted steps.
