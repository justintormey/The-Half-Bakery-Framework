# HEARTBEAT.md — Agent Execution Runbook

## Standard Flow
1. Read your assignment.
2. Use `planning-with-files:plan` if the review covers 3+ distinct areas.
3. Read the project's CLAUDE.md and history.md to understand context and standards.
4. Review systematically: security, correctness, code quality, semver compliance.
5. Use `verification-before-completion` before filing your report.
6. Commit your review artifacts (report file, test results) with messages referencing the issue.
7. Output the ##SUMMARY## block.

## When You Hit an Error
- **Can't run tests (missing deps, broken environment):** Document the exact failure.
  Try an alternative (static analysis, manual code review) rather than abandoning.
- **Ambiguous pass/fail:** Apply the most conservative judgment and document your reasoning.
- **Stuck after 2 attempts:** Use `episodic-memory:remembering-conversations` to find
  prior QA patterns for this project. Then use `systematic-debugging` to diagnose.
- **SSH/network failure or credential prompt:** Output ##BLOCKED## immediately.
  Do not hang waiting for input.

## What "Blocked" Means
##BLOCKED## is NOT "I gave up." It means:
- You tried at least 2 distinct approaches
- You documented what you tried and why each failed
- You identified exactly what a human needs to do to unblock you

Vague ##BLOCKED## messages are a failure mode. Include full error text and attempted steps.
