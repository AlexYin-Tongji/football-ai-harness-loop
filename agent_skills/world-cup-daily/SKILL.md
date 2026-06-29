---
name: world-cup-daily
description: Generate a Chinese daily World Cup report from official match data and time-bounded news evidence. Use for daily recaps, qualification implications, team news, Beijing-time schedules, and viewing priorities.
---

# World Cup Daily Report

Use structured match data as the source of truth for fixtures, scores, tables, and kickoff times. Use news evidence only for narrative context.

## Workflow

1. Read official results, fixtures, stage, and qualification state.
2. Extract and rank material news published before the cutoff.
3. Build a 30-second summary, match recap, implications, team news, and today's Beijing-time schedule.
4. Cite every factual section and expose conflicts or stale data.
5. Review numbers, timezones, team identity, and evidence coverage.

## Boundaries

- Never calculate tables or brackets from prose when structured data exists.
- Never treat an article prediction as a result.
- Keep AI judgment visibly separate from facts.
- Return a report for user editing; do not publish it.

