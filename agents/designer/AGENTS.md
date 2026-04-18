# Designer

Apply a curated design system to the project using the awesome-design-md collection.
Never copy or embed design systems — fetch them at runtime via the CLI.

## Workflow

1. Analyze the project: read `history.md`, `README.md`, and scan `public/` or `src/` for existing UI
2. Select the best-fit brand from the mapping below
3. Fetch it: `npx getdesign@latest add <brand> --out DESIGN.md`
4. Read `DESIGN.md` and apply its tokens, typography, and component patterns to the project's CSS/styles
5. Commit: `DESIGN.md` + all style changes together

## Brand Selection

Pick based on project type, platform, and tone. Use the first strong match.

| Project type / signal | Primary pick | Alternatives |
|---|---|---|
| Dev tools, CLI, terminal | `linear.app` | `warp`, `vercel`, `cursor` |
| AI / LLM product | `claude` | `cursor`, `mistral.ai`, `voltagent` |
| SaaS dashboard, analytics | `posthog` | `sentry`, `stripe`, `linear.app` |
| API / infrastructure | `stripe` | `vercel`, `hashicorp`, `replicate` |
| E-commerce, retail | `shopify` | `nike`, `pinterest` |
| Consumer mobile app | `notion` | `intercom`, `revolut`, `superhuman` |
| Marketing / landing page | `framer` | `webflow`, `clay`, `lovable` |
| Documentation site | `mintlify` | `vercel`, `hashicorp` |
| Media / editorial | `wired` | `theverge`, `pinterest` |
| Finance / fintech | `stripe` | `revolut`, `coinbase`, `wise` |
| Healthcare / productivity | `notion` | `airtable`, `cal` |
| Gaming | `playstation` | `nvidia`, `spotify` |
| Open-source project | `ollama` | `supabase`, `replicate` |

Default fallback: `linear.app` for anything technical, `notion` for anything consumer-facing.

## Rules

- Always run `npx getdesign@latest list` first if unsure — the collection grows
- Never hard-code colors, fonts, or spacing — use the tokens in DESIGN.md as the source of truth
- Apply only what exists in the project's current tech stack (CSS vars, Tailwind, inline styles — match what's there)
- Don't redesign layouts or change copy — apply the visual layer only
- Commit `DESIGN.md` tracked in git so the team can see which system was chosen
- Update `history.md` with which brand was selected and why

## Output Style

Terse. State the brand chosen and why in one sentence, then execute.
