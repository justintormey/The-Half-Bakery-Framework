# Execution Runbook
1. Read assignment
2. Plan if 3+ steps (planning-with-files:plan)
3. Read CLAUDE.md + history.md first
4. Work in focused commits referencing issue number
5. Verify before claiming done (verification-before-completion)
6. Output ##SUMMARY## block

# Errors
- Merge conflict: git status → resolve → add → commit. Never abort.
- Command not found: document failure, try alternatives. No blind retries.
- Stuck after 2 attempts: episodic-memory + systematic-debugging.
- SSH/network/credential prompt: ##BLOCKED## immediately with exact error.
- Tests failing: systematic-debugging (read error → hypothesize → fix → verify).

# ##BLOCKED## Rules
Means: tried 2+ approaches, documented each, identified what human must do.
Include full error text + attempted steps. Vague blockers = failure.
