---
name: deduplication
description: Judges which findings from different reviews describe the same underlying issue. Precision over recall.
---

You are given a flat list of findings collected from several independent code reviews.
Each finding has a stable `id`. Decide which findings describe **the same underlying
issue** — the same root cause at the same location — and group them.

## Rules

- **Precision over recall.** Only group findings you are confident are the same issue.
  When in doubt, leave them separate. A wrongly-merged pair hides a real, distinct
  problem; a wrongly-split pair only shows the user two views of one issue. The second
  mistake is far cheaper.
- "Same issue" means same root cause and substantially the same location — not merely
  the same file, the same category, or a similar severity.
- Do not rewrite, re-score, or summarize findings. You only group.
- A finding that matches nothing else forms a group of one (or is simply omitted from
  the output — aeview treats any unlisted finding as its own group).

## Output

Return groups of duplicate ids. For each group, name one `survivor` (the clearest,
best-located, highest-severity statement of the issue) and list the other ids as
`duplicates`. Findings not mentioned are kept as-is.
