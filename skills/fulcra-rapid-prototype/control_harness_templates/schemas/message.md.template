# Message Schema

Format for any structured message a role writes for the Coordinator or
another role to consume (status updates, handoffs, non-blocking notes).
Distinct from `schemas/verdict.md` (Evaluator-specific) and
`schemas/decision-request.md` (user-judgment escalations specifically).

```text
from: <role>
milestone: <M#>
kind: status | handoff | note
summary: <one concise line>
detail: <as much as needed>
timestamp: <ISO 8601>
```

- `status`: routine progress update, no action required from anyone else.
- `handoff`: durable work state for another session/role to resume from
  (per fulcra-for-agents.md's Durable Handoff pattern) -- goal, current
  state, decisions, unresolved questions, blockers, next useful actions.
- `note`: non-blocking observation worth recording (e.g. an interesting
  finding for `knowledge/`) that doesn't require a decision or block work.

Messages are typically persisted to the Fulcra Workspace team's inbox
paths (`team/<team>/...`), not just left in local files or chat --
durable state is not chat memory (per this skill's own universal
operational invariants).
