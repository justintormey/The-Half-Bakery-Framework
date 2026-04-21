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
- `npx getdesign@latest` fails: retry once, then document exact error + fallback to `npx getdesign@latest list` to confirm brand name. If still failing, ##BLOCKED##.
- No UI surface detected: output one-sentence note ("Project has no UI surface — designer step skipped.") and stop. Not an error.
- DESIGN.md has no Section 9: apply tokens/typography from available sections; note the omission in the commit message.
- Token mismatch (DESIGN.md token not in project stack): adapt to closest equivalent in the existing stack (CSS vars → Tailwind, etc.). Never invent values.

# ##BLOCKED## Rules
Means: tried 2+ approaches, documented each, identified what human must do.
Include full error text + attempted steps. Vague blockers = failure.
